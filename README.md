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

## Developing

```sh
# install the Tauri CLI (root) and the frontend deps (app/)
bun install
bun install --cwd app

# frontend only (browser, no desktop shell)
bun run --cwd app dev

# full desktop app (starts Svelte dev server + Tauri window)
bun run dev

# generate icons from icon.png after editing it
bun run icon
```

## Building the desktop app

```sh
bun install
bun install --cwd app
bun run build
```

See `model/README.md` for the Python side (uv-managed, `uv sync` + `uv run main.py`).