<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { listen, type EventName, type UnlistenFn } from '@tauri-apps/api/event';
  import { Effect, getCurrentWindow } from '@tauri-apps/api/window';
  import Save from '@lucide/svelte/icons/save';

  const ICON_SIZE = 16;
  const NOT_BOUND = 'Not bound';
  const CAPTURE_TRIGGER_TITLE = 'Save capture trigger';
  const REGION_TRIGGER_TITLE = 'Save region trigger';
  const CHOOSE_CAPTURE_TITLE = 'Choose capture trigger';
  const CHOOSE_REGION_TITLE = 'Choose region trigger';
  /// Why the switch is off on a build that will not write the entry. A dev binary
  /// would leave a login entry behind pointing at a build directory, so it is
  /// refused rather than written and the control says so.
  const AUTOSTART_REFUSED_TITLE =
    'A development build never adds itself to your login items';
  const APPEARANCE_BLUR_MAX = 30;
  const APPEARANCE_TINT_MAX = 100;
  const HEX_COLOR = /^#[\da-f]{6}$/i;
  const NATIVE_BLUR_TITLE = 'Native blur';
  /// What the switch says about itself where there is no material to apply. A
  /// control that is off and says nothing reads as broken rather than as
  /// something this desktop does not have, and the row stays on the panel
  /// either way so the reason is one hover away.
  const NATIVE_BLUR_UNSUPPORTED_TITLE =
    "Native blur isn't available on Linux — Tauri exposes no compositor hook. Tint and transparency still apply.";

  /// The look the window draws before the stored appearance answers, and the one
  /// it keeps if the answer never comes, so neither a slow load nor a failed one
  /// is something the user sees. `FALLBACK_ACCENT` is only the
  /// colour the picker opens on — System is the accent actually in force while
  /// `accent` is null.
  const FALLBACK_TINT_PERCENT = 72;
  const FALLBACK_ACCENT = '#3f7fd4';

  type AppearanceTheme = 'system' | 'dark' | 'light';

  /// The desktops whose window server can be asked for a real material. Every
  /// other name is `unsupported`: `setEffects` reaches no compositor there, so
  /// the answer would be a refusal rather than a blur.
  type NativePlatform = 'macos' | 'windows' | 'unsupported';

  type Appearance = {
    accent: string | null;
    blur_px: number;
    tint_opacity: number;
    theme: AppearanceTheme;
  };

  const ACCENT_PRESETS: { name: string; value: string }[] = [
    { name: 'Blue', value: '#3f7fd4' },
    { name: 'Green', value: '#2f9e6e' },
    { name: 'Amber', value: '#c9822f' },
    { name: 'Red', value: '#c0503f' },
    { name: 'Violet', value: '#8a5ad0' },
  ];

  type Trigger = 'capture' | 'region';

  type MonitorInfo = {
    index: number;
    name: string;
    is_primary: boolean;
    width: number;
    height: number;
    x: number;
    y: number;
  };

  /// The state the login entry is now in, and the entry itself. A write answers
  /// only after reading the entry back off the disk, and a read answers from the
  /// disk as well, so what arrives here is the state the desktop actually holds
  /// rather than the one that was asked for. `is_dev` is a refusal, not a state:
  /// a development build writes no entry at all, so the switch is off for a
  /// reason rather than for the entry not being there.
  type AutostartState = {
    enabled: boolean;
    path: string;
    is_dev: boolean;
  };

  /// The backend in use, what it says about the session, and the trigger each
  /// shortcut is actually bound to. A trigger is never read out of the stored
  /// config alone, because a portal session ignores anything typed.
  type HotkeyStatus = {
    backend: 'system' | 'portal';
    detail: string;
    warning: string;
    capture_trigger: string | null;
    select_trigger: string | null;
  };

  let autostart = $state(false);
  let autostartPath = $state('');
  let autostartBusy = $state(false);
  /// Whether the backend refuses the login entry outright. It does in a
  /// development build, which would otherwise leave an entry behind pointing at
  /// a build directory, so the switch is off and the reason is on the panel
  /// rather than in the diagnostics window the user has not opened.
  let autostartRefused = $state(false);
  let monitors = $state<MonitorInfo[]>([]);
  let monitor = $state(0);
  let accent = $state<string | null>(null);
  /// Echoed back on commit so removing the slider does not rot the stored
  /// value; nothing on this page reads it.
  let storedBlurPx = 0;
  let tintOpacity = $state(FALLBACK_TINT_PERCENT);
  let theme = $state<AppearanceTheme>('system');
  /// The desktop is asked once and the switch waits on that answer rather than
  /// guessing, so a build that starts on a desktop with no material never shows
  /// a control it would have to take back.
  let nativePlatform = $state<NativePlatform>('unsupported');
  let nativeBlur = $state(false);
  let nativeBlurBusy = $state(false);
  let hotkeyStatus = $state<HotkeyStatus | null>(null);
  let captureShortcut = $state('');
  let selectShortcut = $state('');
  let savingTrigger = $state<Trigger | null>(null);
  let triggerStatus = $state('');
  const registered: UnlistenFn[] = [];

  /// The diagnostics window is the one place a problem is written down, so a
  /// failure here is handed to it instead of being painted into this panel.
  /// `console` is the only channel left if the handover itself does not get
  /// through, and a silent failure would be the one thing worse than a noisy
  /// message.
  function reportError(source: string, message: string): void {
    void invoke('report_frontend_error', { source, message }).catch(
      (e: unknown) => {
        console.error(`GOaT could not record: ${message}`, e);
      }
    );
  }

  /// A registration that fails hands back no unlisten function, so a rejected
  /// `listen` would be an unhandled rejection and the teardown could never wait
  /// on it. Settling it once here keeps a failure in the diagnostics window and
  /// a success available for the teardown.
  function subscribe<T>(
    event: EventName,
    handler: (payload: T) => void
  ): Promise<void> {
    return listen<T>(event, (e) => handler(e.payload))
      .then((unlisten) => {
        registered.push(unlisten);
      })
      .catch((e: unknown) => {
        reportError('event', `the ${event} feed could not be opened: ${String(e)}`);
      });
  }

  /// The status line is the only account of what the backend actually holds, and
  /// a window-system session reports the stored shortcut itself, so both fields
  /// follow it instead of keeping a second copy that can drift from the trigger
  /// a key press reaches.
  function applyStatus(line: HotkeyStatus): void {
    hotkeyStatus = line;
    if (line.backend !== 'system') return;
    captureShortcut = line.capture_trigger ?? captureShortcut;
    selectShortcut = line.select_trigger ?? selectShortcut;
  }

  // A portal session fires only the trigger its own dialog produced, so nothing
  // typed in this panel is bound there and the row shows what the backend holds.
  const portalBackend = $derived(hotkeyStatus?.backend === 'portal');
  const captureTrigger = $derived(hotkeyStatus?.capture_trigger ?? null);
  const selectTrigger = $derived(hotkeyStatus?.select_trigger ?? null);

  onMount(() => {
    invoke<AutostartState>('is_autostart')
      .then((value) => {
        autostart = value.enabled;
        autostartPath = value.path;
        autostartRefused = value.is_dev;
      })
      .catch((e) => {
        reportError('autostart', String(e));
      });
    invoke<MonitorInfo[]>('list_monitors')
      .then((value) => {
        monitors = value;
      })
      .catch((e) => {
        reportError('monitor', String(e));
      });
    invoke<number>('get_monitor')
      .then((value) => {
        monitor = value;
      })
      .catch((e) => {
        reportError('monitor', String(e));
      });
    void subscribe<HotkeyStatus>('hotkey-status', applyStatus);
    invoke<HotkeyStatus>('hotkey_status')
      .then((value) => {
        applyStatus(value);
      })
      .catch((e) => {
        reportError('hotkey', String(e));
      });
    void subscribe<Appearance>('appearance-changed', applyAppearance);
    invoke<Appearance>('get_appearance')
      .then(applyAppearance)
      .catch((e: unknown) => {
        reportError('appearance', String(e));
      });
    invoke<string>('os_platform')
      .then((value) => {
        nativePlatform = asNativePlatform(value);
      })
      .catch((e: unknown) => {
        reportError('appearance', String(e));
      });
    return () => {
      for (const unlisten of registered) unlisten();
    };
  });

  /// The switch takes the state the desktop actually ended up in, not the one
  /// that was asked for, so a toggle that quietly did nothing cannot look like a
  /// toggle that worked: the command refuses to report a change it could not find
  /// on the disk, and that refusal leaves the switch where it was and the reason
  /// in the diagnostics window. The path under it is written out again on every
  /// press, so it names the entry the desktop holds now.
  async function toggleAutostart(): Promise<void> {
    if (autostartBusy || autostartRefused) return;
    autostartBusy = true;
    try {
      const result = await invoke<AutostartState>('set_autostart', {
        enabled: !autostart,
      });
      autostart = result.enabled;
      autostartPath = result.path;
      autostartRefused = result.is_dev;
    } catch (e) {
      reportError('autostart', String(e));
    } finally {
      autostartBusy = false;
    }
  }

  async function saveMonitor(event: Event): Promise<void> {
    const select = event.currentTarget as HTMLSelectElement;
    try {
      monitor = await invoke<number>('set_monitor', {
        monitor: Number(select.value),
      });
    } catch (e) {
      reportError('monitor', String(e));
    }
  }

  /// Both rows save through the same command: a window-system session registers
  /// the shortcut it is handed, while a portal session ignores it and opens the
  /// desktop's own dialog instead. The reason a row failed belongs to the
  /// diagnostics window, so the status line carries the outcome alone.
  async function saveTrigger(trigger: Trigger): Promise<void> {
    if (savingTrigger) return;
    const isCapture = trigger === 'capture';
    savingTrigger = trigger;
    try {
      const command = isCapture ? 'set_hotkey' : 'set_select_hotkey';
      const held = await invoke<string>(command, {
        hotkey: isCapture ? captureShortcut : selectShortcut,
      });
      if (isCapture) captureShortcut = held;
      else selectShortcut = held;
      triggerStatus = isCapture ? 'Capture trigger saved' : 'Region trigger saved';
    } catch (e) {
      reportError('hotkey', String(e));
      triggerStatus = isCapture
        ? 'Capture trigger not saved'
        : 'Region trigger not saved';
    } finally {
      savingTrigger = null;
    }
  }

  /// The window is hidden rather than closed: the bar raises it again on the
  /// next press, and a closed one would have to be built from scratch to answer.
  async function closeMenu(): Promise<void> {
    try {
      await getCurrentWindow().hide();
    } catch (e) {
      reportError('window', `the menu could not be closed: ${String(e)}`);
    }
  }

  /// The name the desktop reports is believed only for the two desktops whose
  /// window server Tauri can put a material behind. Anything else is a desktop
  /// with no compositor hook to call, and it is left `unsupported` rather than
  /// refused at the press, so the switch can say so before it is touched.
  function asNativePlatform(value: string): NativePlatform {
    return value === 'macos' || value === 'windows' ? value : 'unsupported';
  }

  /// The material each supported desktop is asked for. The two name the same
  /// request differently, so neither name is a guess the other desktop would
  /// understand.
  function nativeBlurEffect(platform: 'macos' | 'windows'): Effect {
    return platform === 'macos' ? Effect.HudWindow : Effect.Acrylic;
  }

  /// The switch takes the state the window actually ended up in, not the one
  /// that was asked for: the material is only marked on once the desktop has
  /// accepted it, and a refusal leaves the switch where it was with the reason
  /// in the diagnostics window.
  ///
  /// The setting is held for this session only. It is a property of the desktop
  /// the window is on rather than of the look the config file records, and the
  /// config has no field for it — so a build that starts without the material
  /// also starts without the blur, which is the state a window is usable in.
  async function toggleNativeBlur(): Promise<void> {
    const platform = nativePlatform;
    if (nativeBlurBusy || platform === 'unsupported') return;
    nativeBlurBusy = true;
    const wanted = !nativeBlur;
    try {
      await getCurrentWindow().setEffects({
        effects: wanted ? [nativeBlurEffect(platform)] : [],
      });
      nativeBlur = wanted;
    } catch (e) {
      reportError('appearance', String(e));
    } finally {
      nativeBlurBusy = false;
    }
  }

  /// A colour the styles cannot read is a string the file happens to hold, not
  /// one the window can draw, and the file is editable by hand — so anything
  /// that is not a six-digit hex colour is taken as the desktop's own.
  function asAccent(value: string | null): string | null {
    return value !== null && HEX_COLOR.test(value) ? value : null;
  }

  /// Both sliders are whole numbers inside the range the menu offers. A file
  /// written by anything other than this menu is the only way a number arrives
  /// that is not one, and `blur(NaNpx)` would void the declaration it stands in
  /// rather than draw a weaker blur.
  function asSliderValue(value: number, max: number): number {
    if (!Number.isFinite(value)) return 0;
    return Math.min(Math.max(Math.round(value), 0), max);
  }

  function asTheme(value: string): AppearanceTheme {
    return value === 'dark' || value === 'light' ? value : 'system';
  }

  /// The look reaches this window three ways — the load on mount, a press in the
  /// menu, and a write from another window arriving as an event — and all three
  /// come through here, so the variables and the panel are never two different
  /// appearances and a value is checked before it reaches either.
  function applyAppearance(loaded: Appearance): void {
    accent = asAccent(loaded.accent);
    storedBlurPx = asSliderValue(loaded.blur_px, APPEARANCE_BLUR_MAX);
    tintOpacity = asSliderValue(loaded.tint_opacity, APPEARANCE_TINT_MAX);
    theme = asTheme(loaded.theme);
  }

  /// The accent a swatch is on, which is the one the picker cannot show as a
  /// preset: it is the only way back to a colour that is not one of them.
  const customAccent = $derived(
    accent !== null && !ACCENT_PRESETS.some((preset) => preset.value === accent)
  );

  /// What the switch names itself. The row is on the panel either way, so the
  /// only thing that changes is whether hovering it explains that there is
  /// nothing here to turn on.
  const nativeBlurTitle = $derived(
    nativePlatform === 'unsupported'
      ? NATIVE_BLUR_UNSUPPORTED_TITLE
      : NATIVE_BLUR_TITLE
  );

  /// The look is two variables and a colour scheme on the document element, so
  /// every rule that reads them repaints without this page re-rendering. The
  /// accent is checked once more on the way out: a value `color-mix` cannot
  /// read voids the whole declaration it stands in, taking the dark fallback
  /// line with it, so the variable is taken away instead and the stylesheet's
  /// own fallbacks keep the window the shape it has.
  ///
  /// System writes nothing at all, because a tree with no `color-scheme` of its
  /// own is what lets the desktop's own popup and scrollbar colours through —
  /// but this page's `color-scheme: dark` is there to stop that popup coming up
  /// white on a dark panel, so removing the inline value hands the decision back
  /// to the stylesheet rather than to the desktop.
  $effect(() => {
    const root = document.documentElement;
    const chosen = asAccent(accent);
    if (chosen) {
      root.style.setProperty('--goat-accent', chosen);
    } else {
      root.style.removeProperty('--goat-accent');
    }
    const tint = asSliderValue(tintOpacity, APPEARANCE_TINT_MAX);
    root.style.setProperty('--goat-tint', `${tint}%`);
    if (theme === 'system') {
      root.style.removeProperty('color-scheme');
    } else {
      root.style.setProperty('color-scheme', theme);
    }
  });

  /// Every control ends up here. The command answers with what it kept, so the
  /// window takes the file's own version of the look rather than the one that
  /// was asked for, and a refusal leaves the menu where it was with the reason
  /// in the diagnostics window.
  async function commitAppearance(): Promise<void> {
    try {
      applyAppearance(
        await invoke<Appearance>('set_appearance', {
          appearance: {
            accent,
            blur_px: storedBlurPx,
            tint_opacity: tintOpacity,
            theme,
          },
        })
      );
    } catch (e) {
      reportError('appearance', String(e));
    }
  }

  function pickAccent(value: string | null): void {
    accent = value;
    void commitAppearance();
  }

  /// A slider moves the window under the thumb on every step and hands the
  /// finished value to the file when the gesture ends, so one drag repaints
  /// continuously and rewrites the config once instead of once per step.
  function readTint(event: Event): void {
    tintOpacity = asSliderValue(
      Number((event.currentTarget as HTMLInputElement).value),
      APPEARANCE_TINT_MAX
    );
  }

  function readCustomAccent(event: Event): void {
    accent = (event.currentTarget as HTMLInputElement).value;
  }

  function saveTheme(event: Event): void {
    theme = asTheme((event.currentTarget as HTMLSelectElement).value);
    void commitAppearance();
  }
</script>

<main class="panel">
  <div class="row">
    <span class="keylabel" title={autostartRefused ? AUTOSTART_REFUSED_TITLE : undefined}>
      Start at login
    </span>
    <button
      class="switch"
      class:checked={autostart}
      role="switch"
      aria-checked={autostart}
      aria-label="Start at login"
      title={autostartRefused ? AUTOSTART_REFUSED_TITLE : 'Start at login'}
      disabled={autostartBusy || autostartRefused}
      onclick={toggleAutostart}
    >
      <span class="knob"></span>
    </button>
  </div>
  {#if autostartRefused}
    <p class="entrypath">{AUTOSTART_REFUSED_TITLE}</p>
  {/if}
  {#if autostartPath}
    <p class="entrypath" title={autostartPath}>{autostartPath}</p>
  {/if}
  <label class="row">
    Monitor
    <select value={monitor} onchange={saveMonitor}>
      {#each monitors as m (m.index)}
        <option value={m.index}>
          {m.name}{m.is_primary ? ' (primary)' : ''} {m.width}x{m.height}
        </option>
      {/each}
    </select>
  </label>

  <div class="keys">
    <div class="row">
      <span class="keylabel">Capture</span>
      {#if portalBackend}
        <span class="bound">{captureTrigger ?? NOT_BOUND}</span>
        <button
          class="choose"
          aria-label={CHOOSE_CAPTURE_TITLE}
          title={CHOOSE_CAPTURE_TITLE}
          onclick={() => saveTrigger('capture')}
          disabled={savingTrigger !== null}
        >
          Choose…
        </button>
      {:else}
        <input
          class="shortcut"
          bind:value={captureShortcut}
          aria-label="Capture trigger"
          placeholder="Ctrl+Shift+S"
        />
        <button
          class="icon"
          aria-label={CAPTURE_TRIGGER_TITLE}
          title={CAPTURE_TRIGGER_TITLE}
          onclick={() => saveTrigger('capture')}
          disabled={savingTrigger !== null}
        >
          <Save size={ICON_SIZE} />
        </button>
      {/if}
    </div>
    <div class="row">
      <span class="keylabel">Region</span>
      {#if portalBackend}
        <span class="bound">{selectTrigger ?? NOT_BOUND}</span>
        <button
          class="choose"
          aria-label={CHOOSE_REGION_TITLE}
          title={CHOOSE_REGION_TITLE}
          onclick={() => saveTrigger('region')}
          disabled={savingTrigger !== null}
        >
          Choose…
        </button>
      {:else}
        <input
          class="shortcut"
          bind:value={selectShortcut}
          aria-label="Region trigger"
          placeholder="Ctrl+Shift+E"
        />
        <button
          class="icon"
          aria-label={REGION_TRIGGER_TITLE}
          title={REGION_TRIGGER_TITLE}
          onclick={() => saveTrigger('region')}
          disabled={savingTrigger !== null}
        >
          <Save size={ICON_SIZE} />
        </button>
      {/if}
    </div>
    {#if triggerStatus}
      <p class="statusline" role="status">{triggerStatus}</p>
    {/if}
  </div>

  <div class="appearance">
    <div class="row">
      <span class="keylabel">Accent</span>
      <button
        class="swatch system"
        class:checked={accent === null}
        aria-pressed={accent === null}
        aria-label="System accent"
        title="System accent"
        onclick={() => pickAccent(null)}
      ></button>
      {#each ACCENT_PRESETS as preset (preset.value)}
        <button
          class="swatch"
          class:checked={accent === preset.value}
          style="background: {preset.value}"
          aria-pressed={accent === preset.value}
          aria-label={`${preset.name} accent`}
          title={`${preset.name} accent`}
          onclick={() => pickAccent(preset.value)}
        ></button>
      {/each}
      <input
        class="swatch custom"
        class:checked={customAccent}
        type="color"
        value={accent ?? FALLBACK_ACCENT}
        aria-label="Custom accent"
        title="Custom accent"
        oninput={readCustomAccent}
        onchange={commitAppearance}
      />
    </div>
    <div class="row">
      <span class="keylabel">Tint</span>
      <input
        class="slider"
        type="range"
        min="0"
        max={APPEARANCE_TINT_MAX}
        value={tintOpacity}
        aria-label="Tint"
        oninput={readTint}
        onchange={commitAppearance}
      />
      <span class="value">{tintOpacity}%</span>
    </div>
    <div class="row">
      <span class="keylabel">{NATIVE_BLUR_TITLE}</span>
      <span class="tipwrap" title={nativeBlurTitle}>
        <button
          class="switch"
          class:checked={nativeBlur}
          role="switch"
          aria-checked={nativeBlur}
          aria-label={NATIVE_BLUR_TITLE}
          disabled={nativePlatform === 'unsupported' || nativeBlurBusy}
          onclick={toggleNativeBlur}
        >
          <span class="knob"></span>
        </button>
      </span>
    </div>
    <label class="row">
      <span class="keylabel">Theme</span>
      <select value={theme} onchange={saveTheme}>
        <option value="system">System</option>
        <option value="dark">Dark</option>
        <option value="light">Light</option>
      </select>
    </label>
  </div>

  <button class="close" onclick={closeMenu}>Close</button>
</main>

<style>
  /* A native dropdown popup is drawn by the desktop rather than by this page, so
     the page tells the whole tree it is dark and the option lists below carry
     their own colours, or the popup can come up white on a dark panel. */
  :global(html) {
    color-scheme: dark;
  }

  /* The window's look lives in variables on the document element, and its
     fallbacks sit in the rules that read them rather than here, because the
     panel is the only surface here. Those fallbacks are the page before the
     stored appearance answers, and the page if it never comes. A chosen accent
     is written over the one below on the document element, and a System accent
     takes that variable away again, which is what leaves the desktop's own
     colour in charge. */
  :global(:root) {
    --goat-accent: AccentColor;
    --goat-tint: 72%;
  }

  :global(body) {
    margin: 0;
    padding: 0;
    background: transparent;
    font-family: system-ui, sans-serif;
    scrollbar-color: rgba(255, 255, 255, 0.3) transparent;
  }

  :global(::-webkit-scrollbar) {
    width: 10px;
    height: 10px;
    background: transparent;
  }

  :global(::-webkit-scrollbar-track) {
    background: transparent;
    border: none;
  }

  :global(::-webkit-scrollbar-thumb) {
    background: rgba(255, 255, 255, 0.25);
    border: 3px solid transparent;
    background-clip: content-box;
    border-radius: 8px;
  }

  :global(::-webkit-scrollbar-corner) {
    background: transparent;
  }

  /* The panel is the whole window rather than something hung out of the bottom
     of a taller one, so it is the root surface: the window is transparent, the
     panel carries the accent tint and blurs whatever the desktop shows through
     it, and it is given the window's height so the tint reaches the bottom edge
     even when the controls do not fill it. A frameless window gets no corner
     rounding from the desktop, so the radius is carried here, and the overflow
     keeps anything the desktop puts in the corner from showing past it. */
  .panel {
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    min-height: 100vh;
    padding: 0.5rem;
    overflow-y: auto;
    color: #fff;
    background: rgba(16, 24, 40, 0.72);
    background: color-mix(
      in srgb,
      var(--goat-accent) var(--goat-tint),
      transparent
    );
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 10px;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.85rem;
    white-space: nowrap;
  }

  .row select {
    min-width: 0;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    background: rgba(255, 255, 255, 0.12);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 0.4rem;
    padding: 0.25rem 0.5rem;
  }

  /* The popup is drawn by the desktop, which ignores the page behind it, so the
     options carry a background of their own. */
  .row select option {
    background: #2a2d33;
    color: #fff;
  }

  .keys {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    padding-top: 0.5rem;
    border-top: 1px solid rgba(255, 255, 255, 0.18);
  }

  /* The appearance controls are the panel's last group, set off by the same
     divider the triggers are, so the groups read the same way down the panel. */
  .appearance {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    padding-top: 0.5rem;
    border-top: 1px solid rgba(255, 255, 255, 0.18);
  }

  /* A swatch is a small plate of the colour it stands for, so the choice is made
     by seeing the colour rather than by reading its value. The chosen one is
     marked by a ring instead of by growing, because a row that changes width
     with the choice would move every swatch after it. */
  .swatch {
    flex: none;
    width: 1.1rem;
    height: 1.1rem;
    padding: 0;
    border: 1px solid rgba(255, 255, 255, 0.35);
    border-radius: 0.3rem;
    cursor: pointer;
  }

  .swatch:hover {
    border-color: #fff;
  }

  .swatch.checked {
    border-color: #fff;
    box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.75);
  }

  .swatch:focus-visible {
    outline: 2px solid #9ec5ff;
    outline-offset: 2px;
  }

  /* System is not a colour of its own, so it is drawn as the two halves no
     palette is made of: what the plate stands for is the desktop's to say, and
     this only marks that the window is following it. */
  .swatch.system {
    background: linear-gradient(135deg, #e8eaee 0 50%, #2a2d33 50% 100%);
  }

  /* The slider takes the accent the window is already wearing, so the control
     that picks the colour is itself that colour and a theme that flips the
     platform's own control palette still leaves it readable on this panel. */
  .slider {
    flex: 1;
    min-width: 0;
    height: 1.1rem;
    margin: 0;
    accent-color: var(--goat-accent);
    cursor: pointer;
  }

  .value {
    min-width: 2.4rem;
    text-align: right;
    color: #c9ccd2;
  }

  .keylabel {
    min-width: 3.4rem;
  }

  .row input.shortcut {
    width: 7.5rem;
    min-width: 0;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    font-size: 0.85rem;
    font-family: inherit;
    background: rgba(255, 255, 255, 0.12);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 0.4rem;
    padding: 0.25rem 0.5rem;
  }

  .bound {
    color: #c9ccd2;
  }

  .choose {
    margin-left: auto;
    background: rgba(255, 255, 255, 0.12);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.3);
    border-radius: 0.4rem;
    padding: 0.25rem 0.6rem;
    font-family: inherit;
    font-size: 0.85rem;
    cursor: pointer;
  }

  .choose:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.22);
  }

  .choose:disabled {
    opacity: 0.4;
    cursor: default;
  }

  /* The switch sits on the right of its row like every other control in the
     panel, and the knob is the only thing that moves, so the row keeps the same
     height as the one above it whether it is on or off. */
  /* A disabled control eats hover, so a tooltip that must fire exactly when
     disabled lives on a wrapper that never disables. */
  .tipwrap {
    display: inline-flex;
    margin-left: auto;
  }

  .row > .switch {
    margin-left: auto;
  }

  .switch {
    position: relative;
    width: 2.2rem;
    height: 1.15rem;
    padding: 0;
    background: rgba(255, 255, 255, 0.12);
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 999px;
    cursor: pointer;
  }

  .switch.checked {
    background: #3f7fd4;
    border-color: #6ba4e6;
  }

  .switch .knob {
    position: absolute;
    top: 0.1rem;
    left: 0.1rem;
    width: 0.85rem;
    height: 0.85rem;
    border-radius: 50%;
    background: #fff;
    transition: transform 0.12s ease-out;
  }

  .switch.checked .knob {
    transform: translateX(1.05rem);
  }

  .switch:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.22);
  }

  .switch.checked:hover:not(:disabled) {
    background: #4f8ade;
  }

  .switch:focus-visible {
    outline: 2px solid #9ec5ff;
    outline-offset: 2px;
  }

  .switch:disabled {
    opacity: 0.4;
    cursor: default;
  }

  /* The line under the switch is the login entry itself, named so the switch is
     not the only claim that the entry is there. It stays on one line and lets a
     long path clip rather than widen the panel off the window. */
  .entrypath {
    margin: -0.15rem 0 0;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: #c9ccd2;
    font-size: 0.75rem;
  }

  /* A save that landed is worth saying out loud here, where the row is; the
     reason one did not belongs to the diagnostics window. */
  .statusline {
    margin: 0;
    color: #c9ccd2;
    font-size: 0.75rem;
  }

  /* The window is dismissed from its own panel as well as by losing focus, so
     the action is on screen rather than something the user has to discover. */
  .close {
    padding: 0.35rem 0;
    background: rgba(255, 255, 255, 0.08);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 0.4rem;
    font-family: inherit;
    font-size: 0.85rem;
    cursor: pointer;
  }

  .close:hover {
    background: rgba(255, 255, 255, 0.18);
  }

  .close:focus-visible {
    outline: 2px solid #9ec5ff;
    outline-offset: 2px;
  }

  .icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.75rem;
    height: 1.75rem;
    padding: 0;
    background: rgba(255, 255, 255, 0.08);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 0.35rem;
    cursor: pointer;
  }

  .icon:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.18);
  }

  .icon:disabled {
    opacity: 0.4;
    cursor: default;
  }
</style>
