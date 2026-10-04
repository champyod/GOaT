<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { listen, type EventName, type UnlistenFn } from '@tauri-apps/api/event';
  import { Effect, getCurrentWindow } from '@tauri-apps/api/window';
  import Save from '@lucide/svelte/icons/save';

  import {
    CAPTURE_PLACEHOLDER,
    GUIDANCE_ID,
    NO_DIALOG_TITLE,
    NOT_BOUND,
    REGION_PLACEHOLDER,
    WAITING_TITLE,
    createKeybindConfig,
    type TriggerRow
  } from '$lib/keybind.svelte';

  const ICON_SIZE = 16;
  const KEY_ESCAPE = 'Escape';
  const KEY_ENTER = 'Enter';
  const KEY_ARROW_UP = 'ArrowUp';
  const KEY_ARROW_DOWN = 'ArrowDown';
  const DROPDOWN_MARK = 'data-dropdown';
  const SAVE_CAPTURE_TITLE = 'Save capture trigger';
  const SAVE_REGION_TITLE = 'Save region trigger';
  const CHOOSE_CAPTURE_TITLE = 'Choose capture trigger';
  const CHOOSE_REGION_TITLE = 'Choose region trigger';
  /// Why the switch is off on a build that will not write the entry. A dev binary
  /// would leave a login entry behind pointing at a build directory, so it is
  /// refused rather than written and the control says so.
  const AUTOSTART_REFUSED_TITLE =
    'A development build never adds itself to your login items';
  const APPEARANCE_BLUR_MAX = 30;
  const APPEARANCE_TINT_MAX = 100;
  /// Where the bar sits before the stored setting answers, and the range the
  /// slider offers. The bar is a top-edge control, so the far end of the range is
  /// only as far down as a window this size can go before it stops being a bar:
  /// a value past the bottom of a laptop screen is a bar nobody can reach, which
  /// is a number worth refusing at the control rather than at the desktop.
  const DEFAULT_BAR_TOP_OFFSET = 28;
  const BAR_TOP_OFFSET_MAX = 500;
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

  type MonitorInfo = {
    index: number;
    name: string;
    is_primary: boolean;
    width: number;
    height: number;
    x: number;
    y: number;
  };

  type DropdownId = 'theme' | 'monitor';

  /// An option is its value as a string because that is what both rows commit
  /// through: the monitor row hands the index to the command as a number, and
  /// the theme row runs the value past the check that decides what the window
  /// will treat as a look.
  type DropdownOption = {
    value: string;
    label: string;
  };

  /// The looks the appearance row offers, in the order it offers them. The label
  /// is the name the button and the list both use, so a theme is picked by the
  /// name the button already shows.
  const THEME_OPTIONS: DropdownOption[] = [
    { value: 'system', label: 'System' },
    { value: 'dark', label: 'Dark' },
    { value: 'light', label: 'Light' },
  ];

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
  let barTopOffset = $state(DEFAULT_BAR_TOP_OFFSET);
  let accent = $state<string | null>(null);
  /// Echoed back on commit so removing the slider does not rot the stored
  /// value; nothing on this page reads it.
  let storedBlurPx = 0;
  let tintOpacity = $state(FALLBACK_TINT_PERCENT);
  let theme = $state<AppearanceTheme>('system');
  /// Which listbox is open, and the option the keyboard cursor is on. One at a
  /// time only: two lists in a panel this narrow would each cover the row the
  /// other one belongs to.
  let openDropdown = $state<DropdownId | null>(null);
  let activeIndex = $state(0);
  /// The desktop is asked once and the switch waits on that answer rather than
  /// guessing, so a build that starts on a desktop with no material never shows
  /// a control it would have to take back.
  let nativePlatform = $state<NativePlatform>('unsupported');
  let nativeBlur = $state(false);
  let nativeBlurBusy = $state(false);
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

  /// The trigger machine this panel shares with the setup window: the backend's
  /// account of the session, the fields behind the two rows, and every state a
  /// row draws itself from. A refusal still goes to the diagnostics window
  /// through `reportError`, and now reads out on the panel under the rows as
  /// well, so the reason is where the press was.
  const keybind = createKeybindConfig({ onError: reportError });
  const view = $derived(keybind.view);

  function fieldValue(row: TriggerRow): string {
    return keybind.field(row);
  }

  function setFieldValue(row: TriggerRow, value: string): void {
    keybind.setField(row, value);
  }

  /// What the pointer reads on a bind button. This panel's own wording names the
  /// row while no round is going, but a round the desktop's own dialog is holding,
  /// and a desktop with no dialog to open at all, both have to say so from the
  /// button itself — a disabled button shows no title of its own, and the wait is
  /// held for both rows at once rather than for the one that asked for it.
  function triggerTitle(own: string): string {
    if (view.saving || view.waiting) return WAITING_TITLE;
    if (view.noShortcutDialog) return NO_DIALOG_TITLE;
    return own;
  }

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
    invoke<number>('get_bar_top_offset')
      .then((value) => {
        barTopOffset = asBarTopOffset(value);
      })
      .catch((e) => {
        reportError('bar', String(e));
      });
    void keybind.start();
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
      keybind.stop();
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

  async function saveMonitor(index: number): Promise<void> {
    try {
      monitor = await invoke<number>('set_monitor', { monitor: index });
    } catch (e) {
      reportError('monitor', String(e));
    }
  }

  /// The stored distance, rounded and held inside the range the slider offers: a
  /// file written by anything other than this menu is the only way a number
  /// arrives that is not one, and a value the slider cannot show is a row reading
  /// something the control itself is not set to.
  function asBarTopOffset(value: number): number {
    if (!Number.isFinite(value)) return DEFAULT_BAR_TOP_OFFSET;
    return Math.min(Math.max(Math.round(value), 0), BAR_TOP_OFFSET_MAX);
  }

  /// The row follows the thumb as it is dragged and hands the finished value to
  /// the file when the gesture ends, so the bar is placed once per drag instead
  /// of once per step. That write is what moves the bar: it answers with the
  /// value the file kept and broadcasts it, so the row shows what was stored and
  /// the bar window is moved to the same number.
  async function commitBarTopOffset(): Promise<void> {
    try {
      barTopOffset = await invoke<number>('set_bar_top_offset', {
        offset: barTopOffset,
      });
    } catch (e) {
      reportError('bar', String(e));
    }
  }

  function readBarTopOffset(event: Event): void {
    barTopOffset = asBarTopOffset(
      Number((event.currentTarget as HTMLInputElement).value)
    );
  }

  /// The monitors as the list names them, which is the text the native option
  /// list carried: the name the desktop reports, the primary mark, and the size.
  const monitorOptions = $derived<DropdownOption[]>(
    monitors.map((m) => ({
      value: String(m.index),
      label: `${m.name}${m.is_primary ? ' (primary)' : ''} ${m.width}x${m.height}`,
    }))
  );

  function optionsOf(which: DropdownId): DropdownOption[] {
    return which === 'theme' ? THEME_OPTIONS : monitorOptions;
  }

  /// The value the window holds for a row, spelled the way the list spells it.
  function chosenValue(which: DropdownId): string {
    return which === 'theme' ? theme : String(monitor);
  }

  function chosenIndex(which: DropdownId, options: DropdownOption[]): number {
    const found = options.findIndex((option) => option.value === chosenValue(which));
    return found < 0 ? 0 : found;
  }

  function chosenLabel(which: DropdownId, options: DropdownOption[]): string {
    return options.at(chosenIndex(which, options))?.label ?? '';
  }

  function isOpen(which: DropdownId): boolean {
    return openDropdown === which;
  }

  function closeDropdown(): void {
    openDropdown = null;
  }

  /// Opening puts the cursor on the choice already in force rather than at the
  /// top of the list, so a keypress moves away from what is held rather than
  /// back to it.
  function toggleDropdown(which: DropdownId, options: DropdownOption[]): void {
    if (isOpen(which)) {
      closeDropdown();
      return;
    }
    openDropdown = which;
    activeIndex = chosenIndex(which, options);
  }

  /// The cursor wraps, because a list of three looks or a handful of monitors has
  /// no end worth stopping at, and Down on the last one means the first.
  function moveActive(options: DropdownOption[], step: number): void {
    if (options.length === 0) return;
    activeIndex = (activeIndex + step + options.length) % options.length;
  }

  /// The list is opened, moved through and committed from the keyboard without
  /// focus ever leaving this window, which is the whole reason it is drawn here.
  /// Enter is taken off the button as well, or the press that commits an option
  /// would also press the button behind it and open the list again.
  function onDropdownKey(
    event: KeyboardEvent,
    which: DropdownId,
    options: DropdownOption[]
  ): void {
    if (event.key === KEY_ESCAPE) {
      closeDropdown();
      return;
    }
    if (event.key === KEY_ARROW_DOWN || event.key === KEY_ARROW_UP) {
      event.preventDefault();
      if (!isOpen(which)) {
        toggleDropdown(which, options);
        return;
      }
      moveActive(options, event.key === KEY_ARROW_DOWN ? 1 : -1);
      return;
    }
    if (event.key === KEY_ENTER && isOpen(which)) {
      event.preventDefault();
      commitDropdown(which, activeIndex);
    }
  }

  /// A pick goes out through the same command the native select's own change
  /// did, so the answer still carries what the file kept and the row still takes
  /// that rather than the value that was asked for.
  function commitDropdown(which: DropdownId, index: number): void {
    const option = optionsOf(which).at(index) ?? null;
    closeDropdown();
    if (option === null) return;
    if (which === 'theme') saveTheme(option.value);
    else void saveMonitor(Number(option.value));
  }

  /// A press anywhere else in the window puts the list away, the way the native
  /// popup used to put the focus away. A press already on a list or on the button
  /// that owns it is left to those, which decide between opening and committing.
  function onWindowPointerDown(event: PointerEvent): void {
    if (openDropdown === null) return;
    const target = event.target;
    if (target instanceof Element && target.closest(`[${DROPDOWN_MARK}]`) !== null) {
      return;
    }
    closeDropdown();
  }

  /// A press on an option must not take the focus with it. The cursor belongs to
  /// the button that owns the list, and a list that closed around a focus it had
  /// stolen would leave the arrows with nothing to answer to.
  function keepFocusOnTrigger(event: MouseEvent): void {
    event.preventDefault();
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

  function saveTheme(value: string): void {
    theme = asTheme(value);
    void commitAppearance();
  }
</script>

<!-- Losing the window is the end of the menu, so a list left open when the
     desktop takes the pointer away from the window is put away with it. -->
<svelte:window onpointerdown={onWindowPointerDown} onblur={closeDropdown} />

{#snippet dropdown(which: DropdownId, label: string, options: DropdownOption[], openUp: boolean)}
  <div class="dropdownwrap" data-dropdown={which}>
    <button
      class="dropdown"
      role="combobox"
      aria-haspopup="listbox"
      aria-expanded={isOpen(which)}
      aria-controls={isOpen(which) ? `${which}-listbox` : undefined}
      aria-activedescendant={isOpen(which) ? `${which}-option-${activeIndex}` : undefined}
      aria-label={label}
      title={chosenLabel(which, options)}
      disabled={options.length === 0}
      onclick={() => toggleDropdown(which, options)}
      onkeydown={(event) => onDropdownKey(event, which, options)}
    >
      <span class="choice">{chosenLabel(which, options)}</span>
      <span class="caret" class:up={isOpen(which)} aria-hidden="true"></span>
    </button>
    {#if isOpen(which)}
      <div
        class="options"
        class:up={openUp}
        id={`${which}-listbox`}
        role="listbox"
        aria-label={label}
      >
        {#each options as option, index (option.value)}
          <button
            type="button"
            class="option"
            class:active={index === activeIndex}
            id={`${which}-option-${index}`}
            role="option"
            tabindex="-1"
            aria-selected={option.value === chosenValue(which)}
            onmousedown={keepFocusOnTrigger}
            onmousemove={() => {
              activeIndex = index;
            }}
            onclick={() => commitDropdown(which, index)}
          >
            {option.label}
          </button>
        {/each}
      </div>
    {/if}
  </div>
{/snippet}

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
  <div class="row">
    Monitor
    {@render dropdown('monitor', 'Monitor', monitorOptions, false)}
  </div>
  <div class="row">
    <span class="keylabel">Bar top</span>
    <input
      class="slider"
      type="range"
      min="0"
      max={BAR_TOP_OFFSET_MAX}
      value={barTopOffset}
      aria-label="Bar top"
      oninput={readBarTopOffset}
      onchange={commitBarTopOffset}
    />
    <span class="value">{barTopOffset}px</span>
  </div>

  <div class="keys">
    <div class="row">
      <span class="keylabel">Capture</span>
      {#if view.portalBackend}
        <span class="bound">{view.captureTrigger ?? NOT_BOUND}</span>
        <button
          class="choose"
          aria-label={CHOOSE_CAPTURE_TITLE}
          aria-describedby={view.guidanceId}
          title={triggerTitle(CHOOSE_CAPTURE_TITLE)}
          onclick={() => keybind.saveTrigger('capture')}
          disabled={view.bindDisabled}
        >
          Choose…
        </button>
      {:else}
        <input
          class="shortcut"
          bind:value={() => fieldValue('capture'), (value) => setFieldValue('capture', value)}
          aria-label="Capture trigger"
          placeholder={CAPTURE_PLACEHOLDER}
        />
        <button
          class="icon"
          aria-label={SAVE_CAPTURE_TITLE}
          aria-describedby={view.guidanceId}
          title={triggerTitle(SAVE_CAPTURE_TITLE)}
          onclick={() => keybind.saveTrigger('capture')}
          disabled={view.bindDisabled}
        >
          <Save size={ICON_SIZE} />
        </button>
      {/if}
    </div>
    <div class="row">
      <span class="keylabel">Region</span>
      {#if view.portalBackend}
        <span class="bound">{view.selectTrigger ?? NOT_BOUND}</span>
        <button
          class="choose"
          aria-label={CHOOSE_REGION_TITLE}
          aria-describedby={view.guidanceId}
          title={triggerTitle(CHOOSE_REGION_TITLE)}
          onclick={() => keybind.saveTrigger('region')}
          disabled={view.bindDisabled}
        >
          Choose…
        </button>
      {:else}
        <input
          class="shortcut"
          bind:value={() => fieldValue('region'), (value) => setFieldValue('region', value)}
          aria-label="Region trigger"
          placeholder={REGION_PLACEHOLDER}
        />
        <button
          class="icon"
          aria-label={SAVE_REGION_TITLE}
          aria-describedby={view.guidanceId}
          title={triggerTitle(SAVE_REGION_TITLE)}
          onclick={() => keybind.saveTrigger('region')}
          disabled={view.bindDisabled}
        >
          <Save size={ICON_SIZE} />
        </button>
      {/if}
    </div>
    {#if view.noShortcutDialog && view.guidanceText}
      <p id={GUIDANCE_ID} class="statusline" role="status">{view.guidanceText}</p>
    {/if}
    {#if view.statusLine}
      <p class="statusline" data-tone={view.statusLineTone} role="status">{view.statusLine}</p>
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
    <div class="row">
      <span class="keylabel">Theme</span>
      {@render dropdown('theme', 'Theme', THEME_OPTIONS, true)}
    </div>
  </div>

  <button class="close" onclick={closeMenu}>Close</button>
</main>

<style>
  /* The controls the desktop draws for itself — the colour picker, the range
     track, the scrollbars — read the page rather than the panel behind them, so
     the page tells the whole tree it is dark, or one of them can come up white
     on a dark panel. */
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

  /* The list is a surface of this panel rather than one of the desktop's, so
     the trigger wears the plate the native select wore and the list below it
     carries the colour the native popup had to be handed by hand. */
  .dropdownwrap {
    position: relative;
    display: inline-flex;
    min-width: 0;
    max-width: 100%;
  }

  .dropdown {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    min-width: 0;
    max-width: 100%;
    background: rgba(255, 255, 255, 0.12);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 0.4rem;
    padding: 0.25rem 0.5rem;
    font-family: inherit;
    font-size: 0.85rem;
    cursor: pointer;
  }

  .dropdown:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.22);
  }

  .dropdown:disabled {
    opacity: 0.4;
    cursor: default;
  }

  .dropdown:focus-visible {
    outline: 2px solid #9ec5ff;
    outline-offset: 2px;
  }

  .choice {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* The mark is drawn rather than imported, so the trigger is a single plate,
     and it turns over with the list the way the switch's knob turns. */
  .caret {
    flex: none;
    border-left: 0.3rem solid transparent;
    border-right: 0.3rem solid transparent;
    border-top: 0.35rem solid rgba(255, 255, 255, 0.75);
    transition: transform 0.12s ease-out;
  }

  .caret.up {
    transform: rotate(180deg);
  }

  /* The list is capped in height and scrolls inside itself, so a desk with a
     monitor on every output does not run the list off the bottom of a window
     that cannot be made taller. */
  .options {
    position: absolute;
    top: 100%;
    left: 0;
    z-index: 1;
    min-width: 100%;
    max-width: 16rem;
    max-height: 11rem;
    overflow: hidden auto;
    background: #2a2d33;
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 0.4rem;
    padding: 0.15rem 0;
  }

  /* The last row on the panel opens upwards, or its list would be drawn over
     the bottom edge of a window that does not grow for it. */
  .options.up {
    top: auto;
    bottom: 100%;
  }

  /* An option is a button so a press on it needs no key event of its own, but
     it is kept out of the tab order: the cursor is the list's, and it is moved
     with the arrows from the button above. */
  .option {
    display: block;
    width: 100%;
    padding: 0.25rem 0.5rem;
    background: transparent;
    color: inherit;
    border: 0;
    font: inherit;
    font-size: 0.85rem;
    text-align: left;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    cursor: pointer;
  }

  /* Two marks, because two things are true of an option: the one in force stays
     bold, and the one the keyboard is on is filled — until a move lands they are
     the same option, which is the point of opening the list on the choice. */
  .option[aria-selected='true'] {
    font-weight: 600;
  }

  .option.active {
    background: rgba(255, 255, 255, 0.18);
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

  /* A save that landed is worth saying out loud here, where the row is, and so is
     a reason one did not: the shared machine reads both out on this line, and the
     tone is what tells them apart at a glance. */
  .statusline {
    margin: 0;
    color: #c9ccd2;
    font-size: 0.75rem;
  }

  .statusline[data-tone='warning'] {
    color: #ffc46b;
  }

  .statusline[data-tone='error'] {
    color: #ff9d9d;
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
