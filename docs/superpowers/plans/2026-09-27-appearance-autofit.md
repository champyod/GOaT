# Appearance Settings + Auto-Fit Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** User-configurable accent color, blur strength, tint opacity and theme in the main menu, plus content-fitted window sizing for the expanded view.

**Architecture:** New `appearance.rs` Rust module owns `AppearanceConfig` (persisted in `UserConfig`); frontend mirrors it into `:root` CSS vars; a debounced `ResizeObserver` drives a clamped `set_window_size` command.

**Tech Stack:** Tauri 2.12, Svelte 5 runes, serde_json, bun.

**Spec:** `docs/superpowers/specs/2026-09-27-appearance-autofit-design.md`

## Global Constraints

- Linux/Wayland blur is CSS `backdrop-filter` only; no compositor API.
- Compact bar stays fixed 300x48; collapsed restore is 300x48.
- Existing `config.json` files without the new field must load unchanged.
- `lib.rs` is 907 lines: new Rust code goes in `appearance.rs`, not `lib.rs`.
- bun only; no new npm dependencies.

## Review Focus

- Old `config.json` without `appearance` loads with defaults (test in Task 1).
- Invalid accent string from config never breaks CSS (sanitize to `None`, test in Task 3).
- Resize feedback loop: `set_size` must not re-trigger an invoke storm (epsilon guard + debounce, test in Task 4).
- `blur_px` 0 renders no blur rather than an invalid filter (test in Task 3).
- Window never exceeds 90% of monitor or drops below 320x200 (test in Task 2).

---

### Task 1: AppearanceConfig backend type

**Files:**
- Create: `src-tauri/src/appearance.rs`
- Modify: `src-tauri/src/lib.rs` (module decl + `UserConfig.appearance` field only)
- Test: `src-tauri/src/appearance.rs` (`#[cfg(test)]`)

**Interfaces:**
- Consumes: `UserConfig` in `lib.rs`
- Produces: `pub struct AppearanceConfig { pub accent: Option<String>, pub blur_px: u8, pub tint_opacity: u8, pub theme: Theme }`, `pub enum Theme { System, Dark, Light }` (serde lowercase), `impl Default` (accent None, blur 14, opacity 62, System), used by Task 2.

- [ ] **Step 1: Write the failing tests** in `appearance.rs`: default values test; deserialize `{"hotkey":"x",...}` without `appearance` yields defaults; unknown theme string falls back to System.
- [ ] **Step 2: Run to verify they fail.** Run: `cargo test --manifest-path src-tauri/Cargo.toml appearance` Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the struct/enum/`Default`/serde attrs in `appearance.rs`; add `mod appearance;` + `appearance: AppearanceConfig` (serde default) to `UserConfig`.
- [ ] **Step 4: Run tests to verify they pass.** Same command. Expected: PASS.
- [ ] **Step 5: Commit.** `git add src-tauri/src/appearance.rs src-tauri/src/lib.rs` + `git commit -m "Add appearance config type with defaults"`.

### Task 2: Appearance + window-size commands

**Files:**
- Modify: `src-tauri/src/appearance.rs` (command fns), `src-tauri/src/lib.rs` (handler registration)
- Test: `src-tauri/src/appearance.rs`

**Interfaces:**
- Consumes: `AppearanceConfig` from Task 1; `save_config`/`load_config` in `lib.rs`.
- Produces: `get_appearance`, `set_appearance(appearance) -> Result<AppearanceConfig, String>` (persists via existing `persist` pattern, emits `appearance-changed`), `set_window_size(width: f64, height: f64)` (clamps 320x200 min, 90% primary monitor max, calls `set_size` on main window). Frontend Task 3/4 invokes these names.

- [ ] **Step 1: Write the failing tests**: clamp test (5000x5000 shrinks to monitor bounds; 10x10 grows to 320x200) via a pure `clamp_size(w, h, mon_w, mon_h)` helper; accent `Some("")` normalizes to `None`.
- [ ] **Step 2: Run to verify they fail.** Same cargo test command. Expected: FAIL.
- [ ] **Step 3: Implement** the helper + three `#[tauri::command]` fns; register all three in `invoke_handler`.
- [ ] **Step 4: Run tests.** Expected: PASS. Also `cargo clippy --manifest-path src-tauri/Cargo.toml -- -D warnings`.
- [ ] **Step 5: Commit.** `git commit -m "Add appearance and window-size commands"`.

### Task 3: Menu appearance UI + CSS vars

**Files:**
- Modify: `app/src/routes/+page.svelte` (menu section, `:root` var application, bar/panel/body styles)
- Test: `bun run --cwd app check` + manual (no frontend unit framework in repo)

**Interfaces:**
- Consumes: `get_appearance`/`set_appearance`/`appearance-changed` from Task 2.
- Produces: CSS vars `--goat-accent`, `--goat-blur`, `--goat-tint` on `:root`; menu section applying them live.

- [ ] **Step 1: Add the Appearance menu section** (swatches incl. System, presets, custom color input; blur 0-30 slider; opacity slider; theme select) wired to `set_appearance`; load via `get_appearance` on mount; subscribe `appearance-changed`.
- [ ] **Step 2: Switch bar/panel/body styles** from hardcoded `AccentColor`/14px to the vars with system fallback; sanitize accent (non-`#rrggbb` → System).
- [ ] **Step 3: Verify.** Run: `bun run --cwd app check` Expected: 0 errors. Manual: change each control, confirm instant apply; restart, confirm persistence.
- [ ] **Step 4: Commit.** `git commit -m "Add appearance settings to menu"`.

### Task 4: Auto-fit expanded view

**Files:**
- Modify: `app/src/routes/+page.svelte` (observer + invoke), uses `set_window_size` from Task 2.

**Interfaces:**
- Consumes: `set_window_size` from Task 2; expanded container element.
- Produces: window sized to expanded content; collapse restores 300x48.

- [ ] **Step 1: Add debounced (~100ms) ResizeObserver** on the expanded container, active only while expanded; invoke `set_window_size` only when size changed by >2px (epsilon guard against feedback loop).
- [ ] **Step 2: Verify.** `bun run --cwd app check` 0 errors. Manual: expand with short text (window small), expand with long OCR text (window grows, never past 90% monitor), collapse (back to 300x48).
- [ ] **Step 3: Commit.** `git commit -m "Auto-fit window to expanded content"`.
