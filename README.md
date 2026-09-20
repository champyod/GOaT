# GOAT

Global Ocr And Translator — desktop app powered by Tauri, SvelteKit, and a Python ML model.

## Layout

The app is split into three components at the repo root:

| Path         | Component  | Stack                                          | Package manager |
| ------------ | ---------- | ---------------------------------------------- | --------------- |
| `app/`       | Frontend   | SvelteKit + Vite + TypeScript                  | bun             |
| `src-tauri/` | Backend    | Rust (Tauri v2 shell + `#[tauri::command]`s)   | cargo           |
| `model/`     | ML model   | Python (OCR/translation inference, training)   | uv              |

- `app/` is self-contained: its own `package.json`, `bun.lock`, and `node_modules` live inside it. The `@tauri-apps/cli` dev dependency also lives here — the root has no JS package files.
- Tauri CLI commands must run from the repo root (that's where `src-tauri/` lives), for example `bunx @tauri-apps/cli tauri dev`.
- `tools/` holds repo-level helper scripts, and `.github/workflows/` runs CI and release builds.
- `icon.png` is the source icon; `src-tauri/icons/` is generated from it by `tauri icon`.

## Developing

```sh
# frontend only (browser, no desktop shell)
bun run --cwd app dev

# full desktop app (starts Svelte dev server + Tauri window)
bunx -p @tauri-apps/cli tauri dev

# generate icons from icon.png after editing it
bunx -p @tauri-apps/cli tauri icon icon.png
```

## Building the desktop app

```sh
bun install --cwd app
bunx -p @tauri-apps/cli tauri build
```

See `model/README.md` for the Python side (uv-managed, `uv sync` + `uv run main.py`).