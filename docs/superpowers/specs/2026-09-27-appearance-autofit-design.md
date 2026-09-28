# Appearance Settings + Auto-Fit Window — Design

Date: 2026-09-27. Status: approved for planning/implementation.

## Intent

User-approved scope (all recommended options): accent + blur sliders in the
main menu panel, Rust-backed config, auto-fit window size for the expanded
view. Compact bar keeps its fixed 300x48.

## Architecture: Rust config + CSS vars

- New `src-tauri/src/appearance.rs` module (lib.rs is 907 lines; no growth):
  `AppearanceConfig { accent: Option<String>, blur_px: u8, tint_opacity: u8,
  theme: Theme }` with serde defaults so existing config.json loads unchanged.
- `UserConfig` gains `appearance: AppearanceConfig` (defaulted).
- Commands `get_appearance` / `set_appearance` + `appearance-changed` event.
  Live apply: frontend mirrors state into `:root` CSS vars
  (`--goat-accent`, `--goat-blur`, `--goat-tint`).
- Bar/panel/body styles switch hardcoded `AccentColor`/blur px to the vars,
  keeping system `AccentColor` as the fallback when accent is System.

## Menu UI

New Appearance section in the existing menu panel of `+page.svelte`:
accent swatches (System + presets + custom `<input type="color">`), blur
slider 0-30px, tint-opacity slider, theme select (system/dark/light).
Writes go through `set_appearance`; UI updates instantly, no save button.

## Auto-fit expanded view

- `ResizeObserver` on the expanded container, debounced ~100ms, only while
  expanded. Reports content size via new `set_window_size { width, height }`.
- Rust clamps to min 320x200 / max 90% of the current monitor, then
  `win.set_size(LogicalSize)`. Collapsing restores 300x48.
- Compact bar and menu-open sizing paths are untouched.

## Limits (stated honestly)

- Tauri exposes no compositor window-blur API on Linux/Wayland. Blur stays
  CSS `backdrop-filter` over the transparent window (already in use); the
  setting tunes its strength only.
- No full theme editor in this scope: accent + blur + opacity + theme only.

## Verification

- `bun run --cwd app check` 0 errors; `cargo test`, clippy, fmt clean.
- Manual: change accent/blur live; expand with long OCR text and confirm the
  window fits; restart and confirm persistence; reduce-motion unaffected.
