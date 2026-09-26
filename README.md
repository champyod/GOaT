# GOAT

Global Ocr And Translator — desktop app powered by Tauri, SvelteKit, and a Python ML model.

## Layout

The app is split into three components at the repo root:

| Path         | Component  | Stack                                          | Package manager |
| ------------ | ---------- | ---------------------------------------------- | --------------- |
| `app/`       | Frontend   | SvelteKit + Vite + TypeScript                  | bun             |
| `src-tauri/` | Backend    | Rust (Tauri v2 shell + `#[tauri::command]`s)   | cargo           |
| `model/`     | ML model   | Python (OCR/translation inference, training)   | uv              |

- The repo root holds one small `package.json` — just the Tauri CLI tool (`@tauri-apps/cli`) — so `bun run tauri <cmd>` works from the root, where `src-tauri/` lives.
- `app/` is self-contained for the frontend: its own `package.json`, `bun.lock`, and `node_modules`.
- `tools/` holds repo-level helper scripts, and `.github/workflows/` runs CI and release builds.
- `icon.png` is the source icon; `src-tauri/icons/` is generated from it by `tauri icon`.
- `sidecar-ocr/` is a small Rust binary shipped as a Tauri sidecar for fallback OCR; `sidecar-ocr/build.sh` builds it and installs it into `src-tauri/binaries/`.

## Developing

```sh
# install the Tauri CLI (root) and the frontend deps (app/)
bun install
bun install --cwd app

# one-time: icons and the OCR sidecar are gitignored, so nothing can bundle
# until both exist — run these before the first `bun run dev` / `bun run build`
bun run icon
bash sidecar-ocr/build.sh

# full desktop app (starts Svelte dev server + Tauri window)
bun run dev

# frontend only (browser, no desktop shell) — needs neither of the two above
bun run --cwd app dev
```

`src-tauri/icons/` and `src-tauri/binaries/` are gitignored, so both are produced locally. `bun run icon` regenerates the icons from `icon.png`; `sidecar-ocr/build.sh` compiles the OCR sidecar into `src-tauri/binaries/tesseract-ocr-<triple>[.exe]`, the filename `externalBin` resolves against. The script primes the tessdata cache with `eng` and `tha`, builds from the crate root, and smoke-tests the installed binary before printing a `triple=` / `installed=` / `size=` / `embedded=eng,tha` summary; re-run it after any change under `sidecar-ocr/`. Build it with the script rather than `cargo build --manifest-path sidecar-ocr/Cargo.toml` — cargo only reads `sidecar-ocr/.cargo/config.toml` from the working directory, so a direct build silently yields an English-only binary.

## Building the desktop app

```sh
bun install
bun install --cwd app
bun run icon
bash sidecar-ocr/build.sh
bun run build
```

See `model/README.md` for the Python side (uv-managed, `uv sync` + `uv run main.py`).