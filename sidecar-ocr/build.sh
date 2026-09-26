#!/usr/bin/env bash
# Build the tesseract-ocr sidecar and install it under src-tauri/binaries.
#
# tesseract-rs 0.4 embeds a language only if its .traineddata is already
# present in the crate's hardcoded cache dir when the build script runs;
# otherwise it prints a cargo:warning and keeps going, silently shipping an
# eng-only binary. The crate pre-seeds that cache from tessdata_best and never
# downloads "tha", so the cache is primed here (pinned to tessdata_fast) before
# cargo is invoked. Without that step the sidecar cannot read Thai at all.
#
# usage: build.sh [--target <rust-target-triple>]
#
# With no argument the sidecar is built for the host triple and installed as
# src-tauri/binaries/tesseract-ocr-<host-triple>[.exe], which is all a Linux or
# Windows runner needs. --target cross-compiles instead and takes the artifact
# from target/<triple>/release/.
#
# A literal empty argument is accepted and means the same thing as no argument.
# The release matrix interpolates its args field unquoted, so an empty field
# reaches this script as no argument at all; the literal empty argument is still
# tolerated for a caller that quotes the value it interpolates.
# macos-latest is an arm64 image, so its two rows cannot both use the host
# default: the x86_64 row has to ask for x86_64-apple-darwin, or both rows emit an
# aarch64 binary and the bundler cannot resolve tesseract-ocr-x86_64-apple-darwin.
set -euo pipefail

# Languages the sidecar must embed: English and Thai.
LANGS="eng tha"
# The crate downloads tessdata_best; tessdata_fast is the same accuracy for UI
# screenshots at a fraction of the size. The stamp file below makes the switch
# deterministic instead of guessing from the existing file size.
TESSDATA_REPO="tessdata_fast"
TESSDATA_BASE_URL="https://github.com/tesseract-ocr/${TESSDATA_REPO}/raw/main/"
TESSDATA_FILENAME_SUFFIX=".traineddata"
FLAVOUR_STAMP=".flavour"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOST_OS="$(uname -s)"

log() { printf '==> %s\n' "$*"; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

# Mirrors tesseract-rs build.rs get_custom_out_dir() platform for platform,
# because a different directory means a cache the crate never reads and an
# eng-only binary. That function reads only the platform environment and offers
# no directory override, so there is nothing to prime other than the path the
# crate itself will use. FreeBSD is matched rather than rejected even though
# nothing here builds for it: the crate resolves it to $HOME/.tesseract-rs, and
# reusing the crate's own path is what makes the cache genuinely shared. An
# unknown platform still dies, exactly as the crate panics there.
resolve_cache_dir() {
    # uname -s reports MINGW64_NT-*/MSYS_NT-*/CYGWIN_NT-* under Git Bash, never
    # the literal string "windows", so the crate's target_os = "windows" arm has
    # to be matched on that prefix.
    case "$HOST_OS" in
        MINGW* | MSYS* | CYGWIN*)
            # The crate's Windows arm has no $HOME fallback: APPDATA wins, and
            # USERPROFILE only contributes the Roaming suffix.
            local appdata="${APPDATA:-}"
            if [ -z "$appdata" ] && [ -n "${USERPROFILE:-}" ]; then
                appdata="${USERPROFILE}\\AppData\\Roaming"
            fi
            [ -n "$appdata" ] ||
                die "APPDATA and USERPROFILE are both unset, and tesseract-rs 0.4 resolves its cache dir from APPDATA (falling back to a Roaming path under USERPROFILE) with no way to override it — export APPDATA for this run"
            printf '%s\n' "${appdata}/tesseract-rs"
            return 0
            ;;
    esac
    local home=""
    case "$HOST_OS" in
        Linux | FreeBSD) if [ -n "${USER:-}" ]; then home="/home/$USER"; fi ;;
        Darwin) if [ -n "${USER:-}" ]; then home="/Users/$USER"; fi ;;
        *)
            die "unsupported platform '${HOST_OS}': tesseract-rs 0.4 defines no cache dir for it, so the tessdata cache cannot be primed"
            ;;
    esac
    if [ -n "${HOME:-}" ]; then
        home="$HOME"
    fi
    [ -n "$home" ] ||
        die "neither HOME nor USER is set, and tesseract-rs 0.4 resolves its cache dir from HOME with a per-user path built from USER as its only fallback — export HOME for this run"
    case "$HOST_OS" in
        Darwin) printf '%s\n' "$home/Library/Application Support/tesseract-rs" ;;
        *) printf '%s\n' "$home/.tesseract-rs" ;;
    esac
}

# The crate's download step never overwrites an existing file, so a cached
# tessdata_best eng.traineddata would survive and win over our fast one. Track
# the flavour explicitly and discard mismatched files instead of inferring it.
frozen_flavour_is_current() {
    [ -f "$1" ] && [ "$(cat "$1")" = "$TESSDATA_REPO" ]
}

download_tessdata() {
    local lang="$1"
    local dest="$2"
    local url="${TESSDATA_BASE_URL}${lang}${TESSDATA_FILENAME_SUFFIX}"
    local part="${dest}.part"
    rm -f "$part"
    curl --fail --silent --show-error --location --retry 3 --output "$part" "$url" || {
        rm -f "$part"
        die "failed to download ${url}"
    }
    [ -s "$part" ] || {
        rm -f "$part"
        die "downloaded ${url} but the file is empty"
    }
    # A captive portal or proxy can answer 200 with an HTML page; a traineddata
    # blob is binary and never starts with '<'.
    [ "$(head -c 1 "$part")" != "<" ] || {
        rm -f "$part"
        die "${url} returned HTML, not tessdata"
    }
    mv "$part" "$dest"
    log "fetched ${lang}${TESSDATA_FILENAME_SUFFIX} ($(du -h "$dest" | cut -f1))"
}

prime_tessdata_cache() {
    local cache_dir="$1"
    local tessdata_dir="${cache_dir}/tessdata"
    local stamp="${tessdata_dir}/${FLAVOUR_STAMP}"
    mkdir -p "$tessdata_dir"
    if frozen_flavour_is_current "$stamp"; then
        log "tessdata cache flavour is ${TESSDATA_REPO}"
    else
        log "tessdata cache is not ${TESSDATA_REPO} (missing or stale stamp) - refetching"
        for lang in "${LANG_LIST[@]}"; do
            rm -f "${tessdata_dir}/${lang}${TESSDATA_FILENAME_SUFFIX}"
        done
    fi
    for lang in "${LANG_LIST[@]}"; do
        local dest="${tessdata_dir}/${lang}${TESSDATA_FILENAME_SUFFIX}"
        if [ -s "$dest" ]; then
            log "cached ${lang}${TESSDATA_FILENAME_SUFFIX} ($(du -h "$dest" | cut -f1))"
            continue
        fi
        download_tessdata "$lang" "$dest"
    done
    printf '%s\n' "$TESSDATA_REPO" >"$stamp"
    log "tessdata cache ${tessdata_dir} is $(du -sh "$tessdata_dir" | cut -f1)"
}

# Cargo picks up .cargo/config.toml from the cwd and its parents, so the crate
# root must be the cwd or the TESSERACT_EMBED_LANGUAGES="eng,tha" override is
# never applied. Building from anywhere else silently falls back to the crate
# default and produces an eng-only binary.
build_release() {
    cd "$SCRIPT_DIR"
    if [ -n "$TARGET_TRIPLE" ]; then
        cargo build --release --target "$TARGET_TRIPLE"
    else
        cargo build --release
    fi
}

# Cargo puts a --target build under target/<triple>/release/ and leaves a host
# build in target/release/. The host layout is kept exactly as it was, because
# that is the path every non-cross-compiling runner has always used.
artifact_dir() {
    if [ -n "$TARGET_TRIPLE" ]; then
        printf '%s/target/%s/release\n' "$SCRIPT_DIR" "$TARGET_TRIPLE"
    else
        printf '%s/target/release\n' "$SCRIPT_DIR"
    fi
}

# Tauri matches sidecar binaries by "<name>-<target triple>". rustc --print
# host-tuple does not exist, so parse the host line out of rustc -vV.
host_triple() {
    local triple
    # MSVC rustc on Windows writes CRLF even into a pipe, which would smuggle a
    # carriage return into the triple and produce a filename Tauri cannot match.
    triple="$(rustc -vV | tr -d '\r' | awk '/^host: /{print $2; exit}')"
    [ -n "$triple" ] || die "could not determine the host triple from 'rustc -vV'"
    printf '%s\n' "$triple"
}

is_windows_target() {
    case "$1" in
        *windows*) return 0 ;;
    esac
    return 1
}

binary_suffix() {
    if is_windows_target "$1"; then
        printf '.exe'
    else
        printf ''
    fi
}

install_sidecar() {
    local built="$1"
    local dest="$2"
    mkdir -p "$(dirname "$dest")"
    # install(1) is coreutils on Linux and BSD on macOS, but the Git Bash
    # environment on windows-latest is not guaranteed to carry it; cp and chmod
    # are present on all three.
    if command -v install >/dev/null 2>&1; then
        install -m755 "$built" "$dest"
    else
        cp "$built" "$dest"
        chmod 755 "$dest"
    fi
}

# One sentence of real text is all that is needed: the point is to prove the
# binary runs and extracts the tessdata it was built with. A solid white image
# is enough, and it keeps this script free of image tooling.
BLANK_PNG_B64='iVBORw0KGgoAAAANSUhEUgAAAMgAAAA8CAIAAACsOWLGAAAAgUlEQVR42u3SQREAAAzCMPybBhXbK5HQawoHIgHGwlgYC4yFsTAWGAtjYSwwFsbCWGAsjIWxwFgYC2OBsTAWxgJjYSyMBcbCWBgLjIWxMBYYC2NhLDAWxsJYYCyMhbHAWBgLY4GxMBbGAmNhLIwFxsJYGAuMhbEwFhgLY2EsMBbfBlBNG5X9QwP8AAAAAElFTkSuQmCC'

decode_base64() {
    # GNU coreutils spells it --decode, BSD/macOS only understands -D.
    if base64 --decode </dev/null >/dev/null 2>&1; then
        base64 --decode
    else
        base64 -D
    fi
}

# Only a native Windows process needs this: MSYS2 translates paths for the shell
# but not for a .exe it launches, so an argv of /tmp/... would arrive as
# C:\tmp\... . cygpath is Git Bash's translator and is not guaranteed on every
# Windows image, so its absence is fatal rather than a silent fallback to a path
# the process cannot open.
to_windows_path() {
    command -v cygpath >/dev/null 2>&1 ||
        die "cygpath is required to address the smoke-test sandbox from a native Windows sidecar but is not on PATH"
    cygpath -w "$1"
}

# Globals rather than return values: the temp-directory variables have to be
# exported in the very shell that later launches the sidecar, and a helper cannot
# hand a value back without a subshell, where those exports would be lost.
SANDBOX=""
PROBE_PNG=""

# Points every temp variable the sidecar might read at a private directory and
# writes the blank probe image there, so the tessdata the binary extracts cannot
# be confused with a copy left behind by an earlier build.
prepare_probe() {
    local triple="$1"
    local sandbox_windows
    SANDBOX="$(mktemp -d)"
    trap 'rm -rf "$SANDBOX"' EXIT
    export TMPDIR="$SANDBOX"
    if is_windows_target "$triple"; then
        # std::env::temp_dir() is TMPDIR on Unix but GetTempPathW on Windows,
        # which reads TEMP and TMP and knows nothing about MSYS2 paths. Spelling
        # the sandbox in Windows form for both variables is what keeps the
        # extraction inside this run's private directory instead of the shared
        # system temp dir, where a tha left by an earlier build would pass.
        sandbox_windows="$(to_windows_path "$SANDBOX")"
        export TEMP="$sandbox_windows"
        export TMP="$sandbox_windows"
    fi
    printf '%s' "$BLANK_PNG_B64" | decode_base64 >"${SANDBOX}/probe.png" ||
        die "failed to generate the smoke-test PNG"
    # A silently truncated decode would otherwise surface as an unreadable-image
    # error from the sidecar, which points at the binary instead of at the probe.
    [ -s "${SANDBOX}/probe.png" ] || die "failed to generate the smoke-test PNG"
    if is_windows_target "$triple"; then
        # MSYS2 translates the paths of the shell's own commands but not the argv
        # of a .exe it launches, so a POSIX probe would arrive as C:\tmp\... .
        PROBE_PNG="$(to_windows_path "${SANDBOX}/probe.png")"
    else
        PROBE_PNG="${SANDBOX}/probe.png"
    fi
}

# tesseract-rs skips missing languages without failing the build, so a green
# cargo build proves nothing. Assert on the files the installed binary actually
# extracts, in a private temp dir so a stale copy from an earlier build cannot
# produce a false pass.
verify_sidecar() {
    local installed="$1"
    local triple="$2"
    local expected lang verified missing
    # An empty language list makes the loop below iterate zero times and then
    # report success, which is the exact failure this function exists to catch.
    # "${LANG_LIST[*]:-}" is the portable emptiness test: the :- operator covers
    # both an unset and an empty array, so it names no unset variable and stays
    # safe under set -u on bash 3.2, where a bare "${LANG_LIST[@]}" expansion of
    # an empty array is an unbound-variable error.
    [ -n "${LANG_LIST[*]:-}" ] ||
        die "internal error: LANG_LIST is empty, so no embedded language can be verified"
    prepare_probe "$triple"
    expected="${SANDBOX}/goat-tessdata"

    # Tesseract writes progress chatter such as "Estimating resolution as N" to
    # stdout, so success is the exit code plus the extracted files, not the text.
    if ! "$installed" "$PROBE_PNG" >"$SANDBOX/out.txt" 2>"$SANDBOX/err.txt"; then
        die "smoke test: sidecar failed ($(cat "$SANDBOX/err.txt"))"
    fi
    # Scalars, not arrays: the passing case is the common one, and testing an
    # empty scalar behaves the same on every bash, expanding an empty array does not.
    verified=""
    missing=""
    for lang in "${LANG_LIST[@]}"; do
        if [ -s "${expected}/${lang}${TESSDATA_FILENAME_SUFFIX}" ]; then
            verified="${verified:+${verified}, }${lang}"
        else
            missing="${missing:+${missing}, }${lang}"
        fi
    done
    [ -z "$missing" ] ||
        die "sidecar is missing embedded tessdata for: ${missing} (re-run this script to rebuild it)"
    # Report what the assertions found on disk, not the languages that were asked for.
    log "smoke test verified embedded tessdata on disk: ${verified}"
}

print_usage() {
    cat <<'USAGE'
usage: build.sh [--target <rust-target-triple>]

Builds the tesseract-ocr sidecar and installs it as
src-tauri/binaries/tesseract-ocr-<triple>[.exe], the filename Tauri resolves
externalBin against.

  --target <triple>  Build for <triple> and read the artifact from
                     target/<triple>/release/. Omit it, or pass an empty
                     argument, to build for the host triple.
  -h, --help         Print this message.
USAGE
}

usage_error() {
    printf 'error: %s\n\n' "$1" >&2
    print_usage >&2
    exit 2
}

# Cargo accepts any string as a --target and only complains once the C++ build
# has already started, so a plausible triple is required here: arch-vendor-os,
# optionally followed by an environment, with both ends drawn from the set
# rustc actually ships.
KNOWN_TARGET_ARCH="aarch64 arm i586 i686 loongarch64 mips mips64 powerpc powerpc64 riscv32 riscv64 s390x thumbv7neon wasm32 x86 x86_64"
KNOWN_TARGET_OS="android darwin freebsd illumos ios linux netbsd openbsd solaris unix unknown windows"

is_known_target_arch() {
    case " ${KNOWN_TARGET_ARCH} " in
        *" $1 "*) return 0 ;;
    esac
    return 1
}

is_known_target_os() {
    case " ${KNOWN_TARGET_OS} " in
        *" $1 "*) return 0 ;;
    esac
    return 1
}

is_plausible_target() {
    local triple="$1" arch vendor os
    case "$triple" in
        *[!a-z0-9_.-]*) return 1 ;;
    esac
    arch="${triple%%-*}"
    [ "$arch" != "$triple" ] || return 1
    vendor="${triple#*-}"
    vendor="${vendor%%-*}"
    os="${triple#*-}"
    os="${os#*-}"
    os="${os%%-*}"
    is_known_target_arch "$arch" && [ -n "$vendor" ] && is_known_target_os "$os"
}

# Both "no argument" and "one empty argument" have to mean host build, because
# the release matrix interpolates its args field unquoted and a quoted caller
# would pass the empty value as an argument. Anything else stops the run, because
# a silently dropped --target would install a sidecar for the wrong architecture
# and fail later in the bundler.
parse_args() {
    TARGET_TRIPLE=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --target=*) TARGET_TRIPLE="${1#--target=}" ;;
            --target)
                [ "$#" -ge 2 ] || usage_error "--target needs a target triple"
                TARGET_TRIPLE="$2"
                shift
                ;;
            -h | --help)
                print_usage
                exit 0
                ;;
            "") ;;
            *) usage_error "unrecognised argument: $1" ;;
        esac
        shift
    done
    if [ -n "$TARGET_TRIPLE" ] && ! is_plausible_target "$TARGET_TRIPLE"; then
        usage_error "'${TARGET_TRIPLE}' is not a target triple (expected arch-vendor-os[-env], e.g. x86_64-apple-darwin)"
    fi
}

main() {
    parse_args "$@"
    read -r -a LANG_LIST <<<"$LANGS"
    local cache_dir triple suffix built binaries_dir installed_path size
    cache_dir="$(resolve_cache_dir)"
    log "tesseract-rs cache: ${cache_dir}"
    prime_tessdata_cache "$cache_dir"
    build_release
    # An explicit --target is also the install name Tauri matches on, so the
    # triple, the .exe suffix and the artifact path all follow the target rather
    # than the machine doing the building.
    if [ -n "$TARGET_TRIPLE" ]; then
        triple="$TARGET_TRIPLE"
    else
        triple="$(host_triple)"
    fi
    suffix="$(binary_suffix "$triple")"
    built="$(artifact_dir)/tesseract-ocr${suffix}"
    [ -x "$built" ] || die "cargo reported success but ${built} is missing"
    binaries_dir="${REPO_ROOT}/src-tauri/binaries"
    installed_path="${binaries_dir}/tesseract-ocr-${triple}${suffix}"
    install_sidecar "$built" "$installed_path"
    verify_sidecar "$installed_path" "$triple"
    size="$(du -h "$installed_path" | cut -f1)"
    printf '\ntriple=%s\ninstalled=%s\nsize=%s\nembedded=%s\n' \
        "$triple" "$installed_path" "$size" "${LANGS// /,}"
}

main "$@"
