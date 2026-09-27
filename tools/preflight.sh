#!/usr/bin/env bash
# preflight: report missing native build tools before tauri dev, tauri build or
# sidecar-ocr/build.sh reaches a cargo build script that fails late and blames the
# wrong thing. tesseract-rs and sentencepiece-sys both shell out to cmake and cc,
# and a missing one surfaces as "is cmake not installed?" from inside a build
# script, long after the shell that could have caught it printed nothing.
#
# Nothing here installs anything: the package manager differs per machine and the user owns the machine, so the script only prints the command to run.
#
# usage: tools/preflight.sh [--for app|sidecar] [--strict] [--quiet]
#
#   --for     check the toolchain one build needs: app (default) or sidecar
#   --strict  fail on a missing recommended tool, not only a missing required one
#   --quiet   print only missing tools, warnings and errors
#
# Written for bash 3.2 because Git Bash on windows-latest ships it: no
# associative arrays, no ${var^^}, no mapfile, no [[ ]].
#
# exit 0  nothing required is missing
# exit 2  a required tool is missing, or --strict turned a warning into a failure
set -euo pipefail

# The app profile is the full Tauri build: the frontend is installed and built
# with bun, Cargo and Tauri both read bun.lock to pick the package manager, and
# pkg-config locates the Tauri-side system libraries. The sidecar profile is what
# sidecar-ocr/build.sh needs for a Rust-only cargo build: cmake and a C++ compiler.
APP_REQUIRED_TOOLS="cmake cargo rustc curl pkg-config bun"
SIDECAR_REQUIRED_TOOLS="cmake cargo rustc curl"
# node is only recommended: bun replaces it for the frontend toolchain, so a
# missing node is worth a warning and nothing more. g++, cc and make are
# recommended because a C++ dependency without a C++ toolchain fails at compile
# time instead of at configure time, which is the late failure this script avoids.
RECOMMENDED_TOOLS="node g++ cc make"

PROFILE=app
STRICT=0
QUIET=0
MISSING_REQUIRED=""
MISSING_RECOMMENDED=""

log() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
fail() { printf 'error: %s\n' "$*" >&2; }

print_usage() {
    cat <<'USAGE'
usage: tools/preflight.sh [--for app|sidecar] [--strict] [--quiet]

  --for     check the toolchain one build needs: app (default, the full
            Tauri build) or sidecar (the Rust-only sidecar-ocr build)
  --strict  fail when a recommended tool is missing, not only a required one
  --quiet   print only missing tools, warnings and errors
  -h        print this message
USAGE
}

usage_error() {
    fail "$1"
    print_usage >&2
    exit 2
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --for)
                [ "$#" -ge 2 ] || usage_error "--for needs a profile: app or sidecar"
                case "$2" in
                    app | sidecar) PROFILE="$2" ;;
                    *) usage_error "unrecognised profile: $2 (expected app or sidecar)" ;;
                esac
                shift
                ;;
            --strict) STRICT=1 ;;
            --quiet) QUIET=1 ;;
            -h | --help)
                print_usage
                exit 0
                ;;
            *) usage_error "unrecognised argument: $1" ;;
        esac
        shift
    done
}

# One tool can legitimately answer to more than one binary name: pkgconf installs
# a binary called pkg-config, and macOS and the BSDs call the C++ compiler c++
# rather than g++. The first name found on PATH is reported and the rest are not.
candidate_names() {
    case "$1" in
        pkg-config) printf '%s' "pkg-config pkgconf" ;;
        g++) printf '%s' "g++ c++ clang++" ;;
        cc) printf '%s' "cc gcc clang" ;;
        *) printf '%s' "$1" ;;
    esac
}

detect_platform() {
    case "$(uname -s)" in
        Linux) printf '%s' "linux" ;;
        Darwin) printf '%s' "macos" ;;
        # uname -s reports MINGW*/MSYS*/CYGWIN* under Git Bash, never "windows".
        MINGW* | MSYS* | CYGWIN*) printf '%s' "windows" ;;
        *) printf '%s' "other" ;;
    esac
}

check_tool() {
    local tool="$1" severity="$2" found="" candidate
    local names
    read -r -a names <<<"$(candidate_names "$tool")"
    for candidate in "${names[@]}"; do
        if command -v "$candidate" >/dev/null 2>&1; then
            found="$candidate"
            break
        fi
    done
    if [ -n "$found" ]; then
        log "ok: ${tool} (${found})"
        return 0
    fi
    if [ "$severity" = "required" ]; then
        printf 'missing: %s\n' "$tool" >&2
        MISSING_REQUIRED="${MISSING_REQUIRED:+${MISSING_REQUIRED} }${tool}"
    else
        warn "${tool} is not on PATH (recommended)"
        MISSING_RECOMMENDED="${MISSING_RECOMMENDED:+${MISSING_RECOMMENDED} }${tool}"
    fi
    return 0
}

# The package that provides a tool, per manager. Windows has none because its hints
# are winget ids and a VS/WebView2 note; an unlisted manager falls back to the name.
package_for() {
    case "$1:$2" in
        cargo:pacman | rustc:pacman) printf '%s' "rust" ;;
        cargo:apt) printf '%s' "cargo" ;;
        rustc:apt) printf '%s' "rustc" ;;
        cargo:brew | rustc:brew) printf '%s' "rust" ;;
        pkg-config:pacman | pkg-config:brew) printf '%s' "pkgconf" ;;
        node:pacman) printf '%s' "nodejs" ;;
        node:brew) printf '%s' "node" ;;
        node:apt) printf '%s' "nodejs" ;;
        bun:brew) printf '%s' "oven-sh/bun/bun" ;;
        *) printf '%s' "$1" ;;
    esac
}

# Tools are one space-separated string, so these loops split on purpose. A package
# already listed is not repeated: pacman ships cargo and rustc as one rust package.
package_list() {
    local tools="$1" manager="$2" list="" tool package
    for tool in $tools; do
        package="$(package_for "$tool" "$manager")"
        case " ${list} " in
            *" ${package} "*) ;;
            *) list="${list:+${list} }${package}" ;;
        esac
    done
    printf '%s' "$list"
}

remaining_tools() {
    local tools="$1" handled="$2" list="" tool
    for tool in $tools; do
        case " ${handled} " in
            *" ${tool} "*) ;;
            *) list="${list:+${list} }${tool}" ;;
        esac
    done
    printf '%s' "$list"
}

# g++, cc and make come from a system toolchain rather than from one package on
# every platform, so package_for's fallback would name a package that does not
# exist and they get a dedicated line instead.
print_toolchain_hints() {
    [ -n "$MISSING_RECOMMENDED" ] || return 0
    case " $MISSING_RECOMMENDED " in
        *" g++ "* | *" cc "* | *" make "*) ;;
        *) return 0 ;;
    esac
    printf 'recommended toolchain (needed by the C++ sidecar dependencies):\n' >&2
    case "$(detect_platform)" in
        linux)
            printf '  arch:   sudo pacman -S --needed base-devel       # cc, g++, make\n' >&2
            printf '  ubuntu: sudo apt-get install -y build-essential  # cc, g++, make\n' >&2
            ;;
        macos) printf '  macos:  xcode-select --install                  # cc, g++, make\n' >&2 ;;
        windows)
            printf '  windows: Visual Studio C++ Build Tools, with the MSVC toolchain rustup\n' >&2
            printf '           defaults to on Windows (cc, g++, make); Microsoft Edge WebView2\n' >&2
            ;;
        *) printf '  install cc, g++ and make from your system C toolchain package\n' >&2 ;;
    esac
}

print_package_hints() {
    [ -n "$MISSING_REQUIRED" ] || return 0
    local rest
    printf 'install the missing build tools, e.g.\n' >&2
    case "$(detect_platform)" in
        linux)
            printf '  arch:   sudo pacman -S --needed %s\n' "$(package_list "$MISSING_REQUIRED" pacman)" >&2
            printf '  ubuntu: sudo apt-get install -y %s\n' "$(package_list "$MISSING_REQUIRED" apt)" >&2
            ;;
        macos)
            printf '  macos:  xcode-select --install\n' >&2
            printf '  macos:  brew install %s\n' "$(package_list "$MISSING_REQUIRED" brew)" >&2
            ;;
        windows)
            # Windows has no distro package for these two, so both lines print whether or not the tool is missing, rather than recommending rustup to someone whose cargo is already on PATH.
            printf '  cmake:  winget install -e --id Kitware.CMake    # if cmake is missing\n' >&2
            printf '  rust:   winget install -e --id Rustlang.Rustup  # if cargo/rustc is missing\n' >&2
            rest="$(remaining_tools "$MISSING_REQUIRED" "cmake cargo rustc")"
            [ -z "$rest" ] || printf '  then install the rest with your Windows package manager: %s\n' "$rest" >&2
            ;;
        *) printf '  install: %s\n' "$(package_list "$MISSING_REQUIRED" none)" >&2 ;;
    esac
}

main() {
    local required tool
    parse_args "$@"
    log "preflight: ${PROFILE} build on $(uname -s) $(uname -m)"
    case "$PROFILE" in
        sidecar) required="$SIDECAR_REQUIRED_TOOLS" ;;
        *) required="$APP_REQUIRED_TOOLS" ;;
    esac
    for tool in $required; do
        check_tool "$tool" required
    done
    for tool in $RECOMMENDED_TOOLS; do
        check_tool "$tool" recommended
    done
    if [ -n "$MISSING_REQUIRED" ]; then
        print_package_hints
        print_toolchain_hints
        fail "preflight failed: ${MISSING_REQUIRED}"
        exit 2
    fi
    if [ "$STRICT" -eq 1 ] && [ -n "$MISSING_RECOMMENDED" ]; then
        print_toolchain_hints
        fail "preflight failed (--strict): ${MISSING_RECOMMENDED}"
        exit 2
    fi
    log "preflight: all required build tools are present"
}

main "$@"
