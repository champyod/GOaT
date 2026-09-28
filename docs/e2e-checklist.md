# E2E manual checklist

The unit suites cannot reach any of this. `app/src/test-setup.ts` stubs the Tauri
IPC, the clipboard, the canvas and the window manager, and the Vitest run in CI
executes under jsdom with no display at all. Everything below is a property of a
real window server, a real compositor, or a real desktop portal — so it is a
person with the built app and a live session.

Run it on the branch under test, not on a packaged release, and note which
desktop and session type each pass was taken on: Wayland and X11 answer these
questions differently, and a pass on one is not evidence about the other.

## How to run

```
bun run tauri dev            # dev build; refuses to write a login entry
bun run tauri build          # packaged build; writes ~/.config/autostart
```

Open the menu and read the hotkey status line before starting. It names the
backend in use — `window-system` or `portal` — and several items below only apply
to one of them.

**Record for every item:** pass / fail / not-applicable, the desktop and
compositor, and the app version. A screenshot of a failure is welcome in the PR
comment; do not commit desktop captures to the repository.

## Known gaps this pass exists to confirm

Each item names the behaviour that is expected and, where the source today does
something else, what it does instead. The second column is not a wish list — it
is read off the code at the commit this checklist was written against, so a pass
and a fail here are both meaningful. Re-read the cited lines before signing off
on a fail: a gap listed in the "today" column is a decision to make, not a
regression to fix on the spot.

---

### 1. A failed capture puts the window back

- **Expected:** the bar comes back and shows the failure. Nothing is left off
  screen with no way to get it back.
- **Today:** `grab_monitor` hides the window, then returns early on a failed
  grab before the `restore_screen` call
  (`src-tauri/src/lib.rs:591`-`599`); `capture_region` does the same
  (`src-tauri/src/lib.rs:617`-`632`). Only the hotkey path restores on failure
  (`src-tauri/src/hotkey/mod.rs:98`-`113`). A button capture that fails to grab
  therefore leaves the window hidden, and the page's own error handling runs in a
  window nobody can see.

**Steps**

1. Start the app. Do not capture yet.
2. Make the grab fail. Either:
   - open the menu, set **Monitor** to a second output, then disable that output
     in the desktop's display settings; or
   - on a Wayland session, deny the screen-capture prompt once.
3. Press the capture button on the bar.

**Pass when** the bar reappears within about a second and names the failure, and
the tray still raises it afterwards.

---

### 2. Diagnostics is reachable before the first capture

- **Expected:** a problem recorded before any capture can be read. Either the
  window opens on its own, or a control in the app opens it.
- **Today:** `show_error_window` returns unless the capture latch is set
  (`src-tauri/src/lib.rs:377`-`390`, latch at `src-tauri/src/lib.rs:34`-`44`),
  and no menu entry or command opens the `errors` window — the menu has no
  diagnostics row. Before the first capture the recorded problems are readable
  only by a user who already knows the route.

**Steps**

1. Start the app and do not capture.
2. Record a failure before any capture. Easiest deterministic one: make the OCR
   models unavailable so `init_models` fails on mount
   (`app/src/routes/+page.svelte:403`-`413`), or start on a session where the
   hotkey backend fails to register.
3. Try to read the log: look through the menu, try the tray, try every keyboard
   shortcut, try relaunching.

**Pass when** the recorded problem can be read without first performing a
successful capture.

---

### 3. Select region on an empty bar is explained

- **Expected:** on a bar with no screenshot, the control is not actionable and
  the reason is available — a disabled state a screen reader can report, with a
  title or an announcement naming what is missing.
- **Today:** the button is disabled only while busy
  (`app/src/routes/+page.svelte:960`-`968`), and pressing it with no screenshot
  returns without a word (`app/src/routes/+page.svelte:764`-`772`). It looks
  available and does nothing.

**Steps**

1. Start the app fresh; the bar shows with no screenshot.
2. Move the pointer over **Select region** and read the tooltip.
3. Click it.
4. With a screen reader running, focus it and read its state.

**Pass when** it cannot be activated and the reason is discoverable without a
screenshot — by tooltip, by accessible name, or by a polite announcement.

---

### 4. A 3-pixel drag is refused with feedback

- **Expected:** a drag too small to be a region is refused *and* the refusal is
  told to the user. Silently dropping it is indistinguishable from a broken
  input path.
- **Today:** both paths treat under 4 px as too small and then return with no
  message — fullscreen drag at `app/src/routes/+page.svelte:854`-`859`, on-screenshot
  drag at `app/src/routes/+page.svelte:903`-`907`.

**Steps**

1. Take a capture so the body is showing.
2. **On-screenshot path:** press **Select region**, press inside the screenshot,
   move exactly 3 px, release.
3. **Fullscreen path:** press the region hotkey, press, move exactly 3 px,
   release.
4. Repeat each with 3 px wide but 200 px tall.

**Pass when** the selection is refused, the overlay comes off, and something
says why.

---

### 5. Close to tray, then raise again, grows the body back

- **Expected:** after a capture, closing to the tray and raising it again brings
  back the grown body over the existing screenshot, not a bare 300 px bar.
- **Today:** `closeToTray` shrinks to the bar and hides
  (`app/src/routes/+page.svelte:685`-`693`); the regrow is driven only by the
  window's focus-changed event (`app/src/routes/+page.svelte:438`-`441` →
  `735`-`738`). If the window never loses focus, or focus is not granted to an
  always-on-top window, that event never fires and the body stays collapsed.
  Regrow is also deliberately skipped while a selection is in progress
  (`app/src/routes/+page.svelte:736`).

**Steps**

1. Take a capture and let the body finish growing.
2. Press **X** on the bar. The window disappears.
3. Raise it: left-click the tray icon.
4. Repeat using the tray's **Show GOaT** menu item.
5. Repeat step 3 with a second monitor attached, raising from the other one.

**Pass when** all three raise paths come back at the grown size with the
screenshot still there.

---

### 6. The region trigger fired mid-drag

- **Expected:** a trigger arriving during a drag either does nothing or ends the
  drag cleanly. It does not silently discard a selection in progress.
- **Today:** the `region-select` handler clears `selStart` and `selRect` and
  switches to the fullscreen surface unconditionally
  (`app/src/routes/+page.svelte:392`-`402`), and `enter_screen_select` goes
  fullscreen and moves the window under it
  (`src-tauri/src/lib.rs:810`-`832`).

**Steps**

1. Take a capture.
2. Press **Select region** and start dragging; hold the button down.
3. Without releasing, press the region hotkey.
4. Release the mouse and observe.

**Pass when** the in-progress selection is either preserved or ended with a
visible change, and the window is not moved or resized under the pointer.

---

### 7. Releasing the mouse outside the webview

- **Expected:** the drag ends, the overlay comes off, and the selection is
  either taken or discarded visibly.
- **Today:** the handlers are on the drag surface itself
  (`app/src/routes/+page.svelte:929`-`936` fullscreen,
  `1000`-`1005` on-screenshot). There is no window-level listener and no pointer
  capture, so a release delivered to another window is never seen and the
  selection stays armed.

**Steps**

1. Press the region hotkey to raise the fullscreen overlay.
2. Press inside the overlay, drag, and release **outside** the GOaT window —
   onto a second monitor, or past the screen edge and back.
3. Confirm with **Esc** afterwards.
4. Repeat the release over a different application's window while GOaT is not
   focused.

**Pass when** the overlay is escapable and the run is not left in a state where
the only way out is a kill.

---

### 8. A bare key is rejected as a trigger

- **Expected:** a trigger with no modifier is refused, with a message naming a
  modifier. The previously bound trigger keeps working, and ordinary typing in
  other applications is unaffected.
- **Today:** `parse_shortcut` accepts a bare key and builds a shortcut with no
  modifiers (`src-tauri/src/hotkey/parse.rs:88`-`111`), and `remap` only checks
  that the string parses (`src-tauri/src/hotkey/mod.rs:140`-`145`).

**Steps**

1. Open the menu and go to **Capture trigger**.
2. Clear the field. Type `A` — no modifier.
3. Save.
4. Then type the letter `a` in a text editor and in a terminal.

**Pass when** the save is refused with a message that names a modifier, the
previously bound trigger still captures, and typing `a` in another application
still types an `a`. Also confirm the saved field is left on the last good value.

Repeat for the region trigger.

---

### 9. Polite announcements, read once

- **Expected:** a screen reader announces each phase of a run once, in order,
  without interrupting whatever the user is already on, and without the field
  covers announcing the same run a second time.
- **Today:** the live region is `aria-live="polite"` and carries the phase
  sentence only (`app/src/routes/+page.svelte:924`), and the covers over the
  fields are hidden from it (`app/src/routes/+page.svelte:918`-`923`).

**Steps**

1. Start a screen reader — Orca on GNOME, Narrator on Windows, VoiceOver on
   macOS.
2. Focus the bar and run one full capture with the trigger.
3. Run a second capture that fails.
4. Focus the **Copy** buttons and copy each field.

**Pass when** each phase is announced once, a failure is announced, the copy
feedback is announced, and nothing is announced twice or cut across the user's
own speech.

---

### 10. The bar under a real window manager

- **Expected:** a transparent, frameless, always-on-top, taskbar-skipped window
  renders as a rounded bar with the tint applied — no black rectangle, no
  window-manager shadow, no compositor outline.
- **Today:** configured that way in `src-tauri/tauri.conf.json:14`-`25`, with
  `visible: false` so the first paint is not flashed at the user. Whether the
  compositor honours the transparency and the always-on-top for a skip-taskbar
  window is a property of the desktop, not of the config.

**Steps**

1. Launch the app. Watch for any flash of a window before the bar is placed.
2. Check the corners are transparent, the tint opacity is applied, and the text
   is legible on a light wallpaper, a dark one, and over a busy one.
3. Open a fullscreen application on another workspace. Confirm the bar is not
   on top of it.
4. Switch workspaces. Confirm the bar behaves as the user expects for a
   skip-taskbar always-on-top window.

**Pass when** the bar renders as designed on the desktop under test, and record
the desktop and compositor with the result.

---

### 11. Tray icon: show and quit

- **Expected:** left-click raises the app; the context menu offers **Show GOaT**
  and **Quit**; **Quit** actually exits, and nothing is left behind.
- **Today:** `setup_tray` wires exactly that
  (`src-tauri/src/lib.rs:775`-`801`).

**Steps**

1. Hide the app with **X**, then left-click the tray icon.
2. Right-click the tray icon and read the menu.
3. Choose **Show GOaT**.
4. Right-click again and choose **Quit**.

**Pass when** all three work, and after **Quit** no GOaT process, no tray icon
and no leftover window remain.

---

### 12. Wayland portal hotkey dialog

- **Only on a Wayland session.** On a window-system session this item is not
  applicable — record it as such rather than skipping it silently.
- **Expected:** the first use of a trigger opens the desktop's own shortcut
  dialog; the app stays usable throughout; cancelling leaves the app running
  with the trigger unbound and says so.
- **Today:** the portal path starts asynchronously at setup
  (`src-tauri/src/hotkey/mod.rs:309`-`340`) and its typed strings are ignored —
  a portal session binds what the desktop hands it
  (`src-tauri/src/hotkey/mod.rs:174`-`177`).

**Steps**

1. Start the app on a fresh Wayland session with no saved triggers.
2. Press the capture hotkey.
3. In the desktop's dialog, bind the keys. Confirm the fields update to what the
   desktop actually bound, not to what was typed.
4. Quit and relaunch, and confirm the bound trigger still works.
5. Repeat from a clean state and **cancel** the dialog instead.

**Pass when** the dialog appears and the recorded triggers match the desktop,
and cancelling leaves a running app with an unbound trigger that says so rather
than a trigger that silently does nothing.
