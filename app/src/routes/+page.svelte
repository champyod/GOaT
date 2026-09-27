<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { listen, type EventName, type UnlistenFn } from '@tauri-apps/api/event';
  import {
    getCurrentWindow,
    LogicalPosition,
    LogicalSize,
  } from '@tauri-apps/api/window';
  import Camera from '@lucide/svelte/icons/camera';
  import ScanLine from '@lucide/svelte/icons/scan-line';
  import Menu from '@lucide/svelte/icons/menu';
  import X from '@lucide/svelte/icons/x';
  import Save from '@lucide/svelte/icons/save';
  import CopyIcon from '@jis3r/icons/icons/copy';
  import CheckIcon from '@jis3r/icons/icons/check';

  const COPIED_FEEDBACK_MS = 1600;
  const ICON_SIZE = 16;
  const BAR_WIDTH = 300;
  const BAR_HEIGHT = 48;
  const BAR_TOP_OFFSET = 28;
  /// The panel is drawn inside the window this height opens to, and anything
  /// below the bottom edge of it is off the window rather than scrollable, so
  /// this has to cover the settings, the triggers and the appearance section
  /// together.
  const MENU_HEIGHT = 380;
  const BODY_WIDTH = 800;
  const BODY_HEIGHT = 600;
  /// The expanded view is fitted once its layout has stopped moving, so a burst
  /// of reflows out of one change is one window resize rather than one per frame.
  const FIT_DEBOUNCE_MS = 100;
  /// The window is only asked to move past this much difference on the leg that
  /// is fitted. A fit that lands within it of the height already asked for is
  /// the same layout measuring itself a fraction differently, and answering that
  /// would walk the window toward the clamp a little at a time.
  const FIT_EPSILON_PX = 2;
  const NOT_BOUND = 'Not bound';
  const CAPTURE_TRIGGER_TITLE = 'Save capture trigger';
  const REGION_TRIGGER_TITLE = 'Save region trigger';
  const CHOOSE_CAPTURE_TITLE = 'Choose capture trigger';
  const CHOOSE_REGION_TITLE = 'Choose region trigger';
  const APPEARANCE_BLUR_MAX = 30;
  const APPEARANCE_TINT_MAX = 100;
  const HEX_COLOR = /^#[\da-f]{6}$/i;

  /// The look the window draws before the stored appearance answers, and the one
  /// it keeps if the answer never comes, so neither a slow load nor a failed one
  /// is something the user sees. `FALLBACK_BLUR_PX` is the strip's own blur
  /// fallback in the stylesheet, and moving one without the other leaves the
  /// slider starting somewhere the window is not. `FALLBACK_ACCENT` is only the
  /// colour the picker opens on — System is the accent actually in force while
  /// `accent` is null.
  const FALLBACK_BLUR_PX = 24;
  const FALLBACK_TINT_PERCENT = 35;
  const FALLBACK_ACCENT = '#3f7fd4';

  type AppearanceTheme = 'system' | 'dark' | 'light';

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

  const STARTS_AT_LOGIN = 'Starts at login';
  const WONT_START_AT_LOGIN = "Won't start at login";
  const LOGIN_UNCHANGED = 'Login setting unchanged';

  type Trigger = 'capture' | 'region';

  type CapturedImage = {
    width: number;
    height: number;
    rgba: number[];
  };

  type MonitorInfo = {
    index: number;
    name: string;
    is_primary: boolean;
    width: number;
    height: number;
    x: number;
    y: number;
  };

  type ResultPayload = {
    image: CapturedImage;
    ocr_text: string;
    translated_text: string;
    ocr_engine: string;
    error: string;
  };

  type ModelsStatus = {
    ocr: boolean;
    nllb: boolean;
    ready: boolean;
  };

  /// The state the login entry is now in, and the entry itself. The backend
  /// reads the entry back off the disk before it answers, so what arrives here
  /// is the state the desktop actually holds rather than what was asked for.
  type AutostartState = {
    enabled: boolean;
    path: string;
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

  let canvasEl: HTMLCanvasElement | undefined = $state();
  let status = $state('Ready');
  let ocrText = $state('');
  let translatedText = $state('');
  let autostart = $state(false);
  let autostartStatus = $state('');
  let autostartBusy = $state(false);
  let busy = $state(false);
  let hasImage = $state(false);
  let monitors = $state<MonitorInfo[]>([]);
  let monitor = $state(0);
  let accent = $state<string | null>(null);
  let blurPx = $state(FALLBACK_BLUR_PX);
  let tintOpacity = $state(FALLBACK_TINT_PERCENT);
  let theme = $state<AppearanceTheme>('system');
  let selecting = $state(false);
  let screenMode = $state(false);
  let menuOpen = $state(false);
  let copiedOcr = $state(false);
  let copiedTranslated = $state(false);
  let hotkeyStatus = $state<HotkeyStatus | null>(null);
  let captureShortcut = $state('');
  let selectShortcut = $state('');
  let savingTrigger = $state<Trigger | null>(null);
  let selStart = $state<{ x: number; y: number } | null>(null);
  let selRect = $state<{ x: number; y: number; w: number; h: number } | null>(
    null
  );
  let imgSize = $state<{ width: number; height: number } | null>(null);
  let selOffset = $state({ x: 0, y: 0 });
  let copiedTimer: ReturnType<typeof setTimeout> | undefined;
  let fitTimer: ReturnType<typeof setTimeout> | undefined;
  /// The size the window was last asked to take. The height is the one the
  /// expanded view is measured against, because the window is what the
  /// measurement moves. The width is never measured: it is held at whatever the
  /// first fit asked for and sent back unchanged, so no fit is ever answered
  /// with its own result.
  let lastFitSize: { width: number; height: number } | null = null;
  let bodyEl: HTMLDivElement | undefined = $state();
  let contentEl: HTMLDivElement | undefined = $state();
  let hasGrown = false;
  let menuGrown = false;
  let barPlaced = false;
  const registered: UnlistenFn[] = [];

  /// The diagnostics window is the one place a problem is written down, so a
  /// failure here is handed to it instead of being painted over the screenshot.
  /// `console` is the only channel left if the handover itself does not get
  /// through, and a silent failure would be the one thing worse than a noisy
  /// overlay.
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

  function draw(image: CapturedImage) {
    if (!canvasEl) return;
    canvasEl.width = image.width;
    canvasEl.height = image.height;
    const ctx = canvasEl.getContext('2d');
    if (!ctx) return;
    const imageData = new ImageData(
      new Uint8ClampedArray(image.rgba),
      image.width,
      image.height
    );
    ctx.putImageData(imageData, 0, 0);
    hasImage = true;
  }

  /// The window starts as a bar and the first capture is what fills it, so the
  /// one moment it can take the body size is the moment the body first appears.
  /// A later capture leaves the window alone, or it would undo a size the user
  /// chose for themselves.
  ///
  /// That same moment gives the desktop's own title bar back, because a window
  /// that is now a whole app rather than a strip is one the title bar belongs
  /// on. A bar-only launch never reaches here, so it stays frameless.
  $effect(() => {
    if (!hasImage || hasGrown) return;
    hasGrown = true;
    const win = getCurrentWindow();
    void win.setDecorations(true).catch((e: unknown) => {
      reportError('window', `the title bar could not be restored: ${String(e)}`);
    });
    void win
      .setSize(new LogicalSize(BODY_WIDTH, BODY_HEIGHT))
      .catch((e: unknown) => {
        reportError('window', `the window could not be resized: ${String(e)}`);
      });
  });

  /// The body and its canvas are behind `hasImage`, so a result that arrives
  /// before the first one would find no canvas to paint. The flag goes up
  /// first and the flush that follows is what puts the canvas in the document,
  /// so every entry point into `draw` paints the frame it was handed.
  async function applyResult(
    result: ResultPayload,
    origin: { x: number; y: number } | null = null
  ): Promise<void> {
    hasImage = true;
    await tick();
    draw(result.image);
    imgSize = { width: result.image.width, height: result.image.height };
    selOffset = origin ?? { x: 0, y: 0 };
    ocrText = result.ocr_text;
    translatedText = result.translated_text;
    if (ocrText.trim()) {
      status = result.ocr_engine
        ? `Result ready (${result.ocr_engine})`
        : 'Result ready';
    } else if (result.error) {
      status = 'Capture incomplete';
    } else {
      status = 'No text detected';
    }
  }

  async function capture(): Promise<void> {
    if (busy) return;
    busy = true;
    status = 'Capturing...';
    try {
      const result = await invoke<ResultPayload>('capture_primary');
      applyResult(result);
    } catch (e) {
      reportError('capture', String(e));
      status = 'Capture failed';
    } finally {
      busy = false;
    }
  }

  /// The raw image of a hotkey capture arrives with no result behind it, and it
  /// is the first thing that can fill an empty window, so it waits for the same
  /// canvas the result path waits for.
  async function drawImage(image: CapturedImage): Promise<void> {
    hasImage = true;
    await tick();
    draw(image);
    imgSize = { width: image.width, height: image.height };
    selOffset = { x: 0, y: 0 };
    status = 'Reading text...';
  }

  onMount(() => {
    void subscribe<ResultPayload>('capture-result', applyResult);
    void subscribe<CapturedImage>('capture-image', drawImage);
    void subscribe('region-select', () => {
      if (monitors.length === 0) {
        reportError('monitor', 'no monitor info is available');
        return;
      }
      selStart = null;
      selRect = null;
      selecting = true;
      screenMode = true;
      window.addEventListener('keydown', cancelSelectOnEsc);
    });
    invoke<ModelsStatus>('models_status')
      .then((value) => {
        if (value.ready) {
          return invoke<string>('init_models');
        }
        return null;
      })
      .then((message) => {
        if (message) status = message;
      })
      .catch((e) => {
        reportError('models', String(e));
      });
    invoke<boolean>('is_autostart')
      .then((value) => {
        autostart = value;
      })
      .catch((e) => {
        reportError('autostart', String(e));
      });
    invoke<MonitorInfo[]>('list_monitors')
      .then((value) => {
        monitors = value;
        void placeBarAtTop(value);
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
    return () => {
      for (const unlisten of registered) unlisten();
      if (copiedTimer) clearTimeout(copiedTimer);
    };
  });

  /// The switch takes the state the desktop actually ended up in, not the one
  /// that was asked for, and the line under it reports that state, so a toggle
  /// that quietly did nothing cannot look like a toggle that worked. The reason
  /// a change did not happen belongs to the diagnostics window.
  async function toggleAutostart(): Promise<void> {
    if (autostartBusy) return;
    autostartBusy = true;
    try {
      const result = await invoke<AutostartState>('set_autostart', {
        enabled: !autostart,
      });
      autostart = result.enabled;
      autostartStatus = result.enabled ? STARTS_AT_LOGIN : WONT_START_AT_LOGIN;
    } catch (e) {
      reportError('autostart', String(e));
      autostartStatus = LOGIN_UNCHANGED;
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

  /// The look reaches the window three ways — the load on mount, a press in the
  /// menu, and a write from another window arriving as an event — and all three
  /// come through here, so the variables and the menu are never two different
  /// appearances and a value is checked before it reaches either.
  function applyAppearance(loaded: Appearance): void {
    accent = asAccent(loaded.accent);
    blurPx = asSliderValue(loaded.blur_px, APPEARANCE_BLUR_MAX);
    tintOpacity = asSliderValue(loaded.tint_opacity, APPEARANCE_TINT_MAX);
    theme = asTheme(loaded.theme);
  }

  /// The accent a swatch is on, which is the one the picker cannot show as a
  /// preset: it is the only way back to a colour that is not one of them.
  const customAccent = $derived(
    accent !== null && !ACCENT_PRESETS.some((preset) => preset.value === accent)
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
    const blur = asSliderValue(blurPx, APPEARANCE_BLUR_MAX);
    const tint = asSliderValue(tintOpacity, APPEARANCE_TINT_MAX);
    root.style.setProperty('--goat-blur', `${blur}px`);
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
            blur_px: blurPx,
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
  function readBlur(event: Event): void {
    blurPx = asSliderValue(
      Number((event.currentTarget as HTMLInputElement).value),
      APPEARANCE_BLUR_MAX
    );
  }

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

  /// The window is a 48px bar until a capture fills it, so the panel has
  /// nowhere to open into until the window is given the height the menu needs.
  /// The width is read back rather than assumed, so a window the user widened
  /// keeps that width while the menu is open.
  async function growWindowForMenu(): Promise<void> {
    const win = getCurrentWindow();
    try {
      const [size, scale] = await Promise.all([
        win.outerSize(),
        win.scaleFactor(),
      ]);
      const width = size.toLogical(scale).width;
      await win.setSize(new LogicalSize(width, MENU_HEIGHT));
      menuGrown = true;
    } catch (e) {
      reportError('window', `the window could not open the menu: ${String(e)}`);
    }
  }

  /// The bar is meant to sit along the top of the primary monitor, centred, and
  /// the window otherwise comes up wherever the desktop put it. Monitor geometry
  /// arrives in physical pixels while `setPosition` takes logical ones, so every
  /// distance goes through the window's own scale factor, the same way the menu
  /// grow reads its width back. The move happens once: a bar the user has since
  /// dragged somewhere is theirs, and re-placing it would take it away again.
  async function placeBarAtTop(list: MonitorInfo[]): Promise<void> {
    if (barPlaced) return;
    barPlaced = true;
    const win = getCurrentWindow();
    try {
      const [size, scale] = await Promise.all([
        win.outerSize(),
        win.scaleFactor(),
      ]);
      const primary = list.find((m) => m.is_primary) ?? list[0];
      const monitorWidth = primary ? primary.width / scale : window.screen.width;
      const monitorLeft = primary ? primary.x / scale : 0;
      const barWidth = size.toLogical(scale).width;
      const x = Math.round(monitorLeft + (monitorWidth - barWidth) / 2);
      await win.setPosition(new LogicalPosition(x, BAR_TOP_OFFSET));
    } catch (e) {
      reportError('window', `the bar could not be placed: ${String(e)}`);
    }
  }

  /// Only a grow this page performed is taken back, so a window the user
  /// resized, and the size the first capture needed, are both left alone.
  async function shrinkWindowFromMenu(): Promise<void> {
    if (!menuGrown) return;
    menuGrown = false;
    try {
      await getCurrentWindow().setSize(new LogicalSize(BAR_WIDTH, BAR_HEIGHT));
    } catch (e) {
      reportError('window', `the window could not be resized: ${String(e)}`);
    }
  }

  /// Once the body is there the window is already taller than the menu needs,
  /// and the capture grow owns its size.
  async function resizeForMenu(open: boolean): Promise<void> {
    if (hasImage || hasGrown) return;
    if (open) await growWindowForMenu();
    else await shrinkWindowFromMenu();
  }

  function toggleMenu(): void {
    menuOpen = !menuOpen;
    void resizeForMenu(menuOpen);
  }

  function closeMenu(): void {
    menuOpen = false;
    void resizeForMenu(false);
  }

  /// The height the window has to be for the expanded view to fit whole, or null
  /// when there is nothing laid out to measure. The body is stretched to the
  /// window, so its own box is the window read back and says nothing about the
  /// room the content wants; the content grid is the one box in that column laid
  /// out at its natural size, and everything from the top of the document down
  /// to the padding under it is inside the window. Offsets stand in for a client
  /// rect because they read the same whether or not the view is scrolled.
  function expandedContentHeight(): number | null {
    if (!bodyEl || !contentEl) return null;
    const paddingBelow = parseFloat(getComputedStyle(bodyEl).paddingBottom);
    const height = contentEl.offsetTop + contentEl.offsetHeight + paddingBelow;
    if (!Number.isFinite(height)) return null;
    // A read taken before the view has been laid out comes back as nothing, and
    // asking for a window of nothing is how a fit ends up at the clamp rather
    // than at the content.
    if (height <= 0) return null;
    return height;
  }

  /// The height is fitted and the width is sent back exactly as it was last
  /// asked for. Fitting the width would measure the body, and the body is the
  /// window: a fit that reaches the height clamp brings a scrollbar with it, the
  /// body loses the width of that scrollbar, and the narrower window handed back
  /// for it is that same walk one step on. The desktop clamps whatever width it
  /// is given, so the frozen one is safe to keep sending.
  ///
  /// A refusal is written down rather than raised: the view works at whatever
  /// size it has, so a window the desktop will not take is a report and not a
  /// failure. The size asked for is kept either way, so a layout that keeps
  /// reading the same does not keep asking.
  async function fitWindowToContent(height: number): Promise<void> {
    const width = lastFitSize?.width ?? bodyEl?.offsetWidth;
    if (width === undefined || width <= 0) return;
    lastFitSize = { width, height };
    try {
      await invoke('set_window_size', { width, height });
    } catch (e) {
      reportError('window', `the window could not be fitted: ${String(e)}`);
    }
  }

  /// One fit per settled layout, not one per observation: a capture, a reflow
  /// and a scrollbar appearing all arrive as several sizes of the same view, and
  /// each of them would otherwise be a window the user watches move. Only the
  /// height is compared, because the width is sent back as it was and so cannot
  /// have moved.
  function scheduleWindowFit(): void {
    if (fitTimer) clearTimeout(fitTimer);
    fitTimer = setTimeout(() => {
      fitTimer = undefined;
      const height = expandedContentHeight();
      if (!height) return;
      const asked = lastFitSize;
      if (asked && Math.abs(height - asked.height) <= FIT_EPSILON_PX) return;
      void fitWindowToContent(height);
    }, FIT_DEBOUNCE_MS);
  }

  /// The expanded view is watched for exactly as long as it is on screen. The
  /// menu sizes the window by itself and before the panel is in the document, so
  /// that path is left alone rather than answered to.
  $effect(() => {
    const target = hasImage ? contentEl : undefined;
    if (!target) return;
    const observer = new ResizeObserver(() => scheduleWindowFit());
    observer.observe(target);
    return () => {
      observer.disconnect();
      if (fitTimer) {
        clearTimeout(fitTimer);
        fitTimer = undefined;
      }
    };
  });

  /// The bar is the title bar, so closing it hides the window instead of ending
  /// the process: the hotkeys are registered outside the window and a quit
  /// would take them with it.
  async function closeToTray(): Promise<void> {
    try {
      await invoke('hide_window');
    } catch (e) {
      reportError('window', `the window could not be hidden: ${String(e)}`);
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
      status = isCapture ? 'Capture trigger saved' : 'Region trigger saved';
    } catch (e) {
      reportError('hotkey', String(e));
      status = isCapture ? 'Capture trigger not saved' : 'Region trigger not saved';
    } finally {
      savingTrigger = null;
    }
  }

  /// Both panels share one timer so a second copy restarts the feedback rather
  /// than leaving the first one to clear an icon that already moved on.
  function flashCopied(field: 'ocr' | 'translated'): void {
    if (field === 'ocr') copiedOcr = true;
    else copiedTranslated = true;
    if (copiedTimer) clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => {
      copiedOcr = false;
      copiedTranslated = false;
    }, COPIED_FEEDBACK_MS);
  }

  async function copyText(
    text: string,
    field: 'ocr' | 'translated'
  ): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      flashCopied(field);
    } catch (e) {
      reportError('clipboard', String(e));
    }
  }

  function startSelect(): void {
    if (!hasImage) {
      status = 'Capture a screenshot first, then drag on it to select a region';
      return;
    }
    selStart = null;
    selRect = null;
    selecting = true;
    window.addEventListener('keydown', cancelSelectOnEsc);
  }

  function stopSelectMode() {
    const wasScreen = screenMode;
    selecting = false;
    screenMode = false;
    selStart = null;
    selRect = null;
    window.removeEventListener('keydown', cancelSelectOnEsc);
    if (wasScreen) {
      const win = getCurrentWindow();
      win.isFullscreen().then((full) => {
        if (full) {
          win.setFullscreen(false);
        }
      });
    }
  }

  function cancelSelectOnEsc(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      stopSelectMode();
    }
  }

  function canvasPos(event: MouseEvent) {
    const wrap = event.currentTarget as HTMLDivElement;
    const box = wrap.getBoundingClientRect();
    return { x: event.clientX - box.left, y: event.clientY - box.top };
  }

  function onSelDown(event: MouseEvent) {
    if (!selecting) return;
    let p: { x: number; y: number };
    if (screenMode) {
      p = { x: event.clientX, y: event.clientY };
    } else {
      p = canvasPos(event);
    }
    selStart = p;
    selRect = { x: p.x, y: p.y, w: 0, h: 0 };
  }

  function onSelMove(event: MouseEvent) {
    if (!selecting || !selStart) return;
    let cx: number;
    let cy: number;
    let maxW: number;
    let maxH: number;
    if (screenMode) {
      cx = event.clientX;
      cy = event.clientY;
      maxW = window.innerWidth;
      maxH = window.innerHeight;
    } else {
      if (!canvasEl) return;
      const box = canvasEl.getBoundingClientRect();
      cx = event.clientX - box.left;
      cy = event.clientY - box.top;
      maxW = box.width;
      maxH = box.height;
    }
    const x = Math.max(0, Math.min(selStart.x, cx));
    const y = Math.max(0, Math.min(selStart.y, cy));
    selRect = {
      x,
      y,
      w: Math.max(0, Math.min(cx, maxW) - x),
      h: Math.max(0, Math.min(cy, maxH) - y),
    };
  }

  async function finishScreenSelect(): Promise<void> {
    const rect = selRect;
    const mon = monitors.find((m) => m.index === monitor) ?? monitors[0];
    const scale = await getCurrentWindow().scaleFactor();
    const tooSmall = !rect || rect.w < 4 || rect.h < 4;
    stopSelectMode();
    if (tooSmall || !rect || !mon) {
      if (!mon) reportError('monitor', 'no monitor info is available');
      else status = 'Selection too small';
      return;
    }
    const x = Math.max(0, Math.round(rect.x * scale + mon.x));
    const y = Math.max(0, Math.round(rect.y * scale + mon.y));
    const width = Math.max(1, Math.round(rect.w * scale));
    const height = Math.max(1, Math.round(rect.h * scale));
    busy = true;
    status = 'Capturing region...';
    try {
      const result = await invoke<ResultPayload>('capture_region', {
        monitor,
        x,
        y,
        width,
        height,
      });
      applyResult(result);
    } catch (e) {
      reportError('capture', String(e));
      status = 'Capture failed';
    } finally {
      busy = false;
    }
  }

  async function onSelUp(): Promise<void> {
    if (!selecting || !selRect || busy) {
      return;
    }
    if (screenMode) {
      await finishScreenSelect();
      return;
    }
    if (!canvasEl || !imgSize) {
      return;
    }
    const box = canvasEl.getBoundingClientRect();
    const scaleX = imgSize.width / box.width;
    const scaleY = imgSize.height / box.height;
    const x = Math.min(
      imgSize.width - 1,
      Math.max(0, Math.round(selRect.x * scaleX))
    );
    const y = Math.min(
      imgSize.height - 1,
      Math.max(0, Math.round(selRect.y * scaleY))
    );
    const width = Math.max(
      1,
      Math.min(Math.round(selRect.w * scaleX), imgSize.width - x)
    );
    const height = Math.max(
      1,
      Math.min(Math.round(selRect.h * scaleY), imgSize.height - y)
    );
    const fullX = selOffset.x + x;
    const fullY = selOffset.y + y;
    const tooSmall = selRect.w < 4 || selRect.h < 4;
    stopSelectMode();
    if (tooSmall) {
      status = 'Selection too small';
      return;
    }
    busy = true;
    status = 'Reading selection...';
    try {
      const result = await invoke<ResultPayload>('ocr_selection', {
        x: fullX,
        y: fullY,
        width,
        height,
      });
      applyResult(result, { x: fullX, y: fullY });
    } catch (e) {
      reportError('ocr', String(e));
      status = 'Selection failed';
    } finally {
      busy = false;
    }
  }
</script>

<main class:has-body={hasImage}>
  {#if selecting && screenMode}
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions --
      Full-screen drag surface; Esc to cancel is on window keydown. -->
    <div
      class="overlay"
      role="application"
      aria-label="Drag to select a screen region"
      onmousedown={onSelDown}
      onmousemove={onSelMove}
      onmouseup={onSelUp}
    >
      <p class="overlay-hint">Drag to select a region. Esc cancels.</p>
      {#if selRect}
        <div
          class="selrect screen"
          style="left: {selRect.x}px; top: {selRect.y}px; width: {selRect.w}px; height: {selRect.h}px;"
        ></div>
      {/if}
    </div>
  {:else}
    <div class="bar">
      <div class="actions">
        <button
          class="icon"
          onclick={capture}
          disabled={busy}
          aria-label="Capture"
          title="Capture"
        >
          <Camera size={ICON_SIZE} />
        </button>
        <button
          class="icon"
          onclick={startSelect}
          disabled={busy || !hasImage}
          aria-label="Select region"
          title={hasImage ? 'Select region' : 'Capture a screenshot first'}
        >
          <ScanLine size={ICON_SIZE} />
        </button>
      </div>
      <div class="actions">
        <button
          class="icon"
          onclick={toggleMenu}
          aria-label="Menu"
          title="Menu"
          aria-expanded={menuOpen}
        >
          <Menu size={ICON_SIZE} />
        </button>
      </div>
      <div class="grip" data-tauri-drag-region aria-hidden="true"></div>
      <div class="actions trailing">
        <button
          class="icon ghost"
          onclick={closeToTray}
          aria-label="Close"
          title="Close (hide to tray)"
        >
          <X size={ICON_SIZE} />
        </button>
      </div>
    </div>

    {#if menuOpen}
      <button
        class="scrim"
        tabindex="-1"
        aria-label="Close menu"
        onclick={closeMenu}
      ></button>
      <div class="panel">
        <div class="row">
          <span class="keylabel">Start at login</span>
          <button
            class="switch"
            class:checked={autostart}
            role="switch"
            aria-checked={autostart}
            aria-label="Start at login"
            title="Start at login"
            disabled={autostartBusy}
            onclick={toggleAutostart}
          >
            <span class="knob"></span>
          </button>
        </div>
        {#if autostartStatus}
          <p class="switchline" role="status">{autostartStatus}</p>
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
            <span class="keylabel">Blur</span>
            <input
              class="slider"
              type="range"
              min="0"
              max={APPEARANCE_BLUR_MAX}
              value={blurPx}
              aria-label="Blur"
              oninput={readBlur}
              onchange={commitAppearance}
            />
            <span class="value">{blurPx}px</span>
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
          <label class="row">
            <span class="keylabel">Theme</span>
            <select value={theme} onchange={saveTheme}>
              <option value="system">System</option>
              <option value="dark">Dark</option>
              <option value="light">Light</option>
            </select>
          </label>
        </div>

        {#if portalBackend}
          <p class="note">
            Shortcuts live in the desktop's own settings, and these buttons open
            that window. Anything that fails to save goes to the GOaT Errors
            window.
          </p>
        {/if}
      </div>
    {/if}

    {#if hasImage}
    <div class="body" bind:this={bodyEl}>
      <p class="status" role="status">{status}</p>

      <div class="content" bind:this={contentEl}>
        <section class="shot">
          <h2>Screenshot</h2>
          <!-- svelte-ignore a11y_no_noninteractive_element_interactions --
            Drag surface over the screenshot; Esc to cancel is on window keydown. -->
          <div
            class="shotwrap"
            class:armed={selecting}
            role="application"
            aria-label="Drag on the screenshot to select a region"
            onmousedown={onSelDown}
            onmousemove={onSelMove}
            onmouseup={onSelUp}
          >
            <canvas bind:this={canvasEl}></canvas>
            {#if selRect}
              <div
                class="selrect"
                style="left: {selRect.x}px; top: {selRect.y}px; width: {selRect.w}px; height: {selRect.h}px;"
              ></div>
            {/if}
          </div>
          {#if selecting}
            <p class="placeholder">Drag to select. Esc cancels.</p>
          {/if}
        </section>

        <div class="side">
          <section>
            <div class="head">
              <h2>OCR'ed text</h2>
              <button
                class="icon"
                onclick={() => copyText(ocrText, 'ocr')}
                disabled={!ocrText}
                aria-label="Copy OCR'ed text"
                title="Copy"
              >
                {#if copiedOcr}
                  <CheckIcon size={ICON_SIZE} animate={true} />
                {:else}
                  <CopyIcon size={ICON_SIZE} animate={false} />
                {/if}
              </button>
            </div>
            <textarea bind:value={ocrText} placeholder="Empty" rows={6}
            ></textarea>
          </section>
          <section>
            <div class="head">
              <h2>Translated text</h2>
              <button
                class="icon"
                onclick={() => copyText(translatedText, 'translated')}
                disabled={!translatedText}
                aria-label="Copy translated text"
                title="Copy"
              >
                {#if copiedTranslated}
                  <CheckIcon size={ICON_SIZE} animate={true} />
                {:else}
                  <CopyIcon size={ICON_SIZE} animate={false} />
                {/if}
              </button>
            </div>
            <textarea
              bind:value={translatedText}
              placeholder="Empty"
              rows={6}
            ></textarea>
          </section>
        </div>
      </div>
    </div>
    {/if}
  {/if}
</main>

<style>
  /* A native dropdown popup is drawn by the desktop rather than by this page, so
     the page tells the whole tree it is dark and the option list below carries
     its own colours, or the popup can come up white on a white window. */
  :global(html) {
    color-scheme: dark;
  }

  /* The window's look lives in variables on the document element, and its
     fallbacks sit in the rules that read them rather than here, because the
     strip and the body have always drawn at different blur strengths and one
     shared fallback would flatten that. Those fallbacks are the page before the
     stored appearance answers, and the page if it never comes. A chosen accent
     is written over the one below on the document element, and a System accent
     takes that variable away again, which is what leaves the desktop's own
     colour in charge. */
  :global(:root) {
    --goat-accent: AccentColor;
    --goat-tint: 35%;
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

  main {
    position: relative;
    display: flex;
    flex-direction: column;
    color: #fff;
  }

  /* The window is only tall enough for the bar until a capture fills it, so the
     full-height column is scoped to the body and the bar sits on transparency
     instead of stretching over the rest of the window. */
  main.has-body {
    min-height: 100vh;
  }

  /* There is no title bar of the desktop's own, so this strip stands in for one.
     The window is transparent, so the strip carries a translucent tint and blurs
     whatever the desktop shows through it rather than sitting on an opaque plate,
     and it stays a fixed-height row, which leaves the remainder of the bar-only
     window transparent rather than painted.

     The tint is the desktop's own accent colour, so the strip belongs to the
     system the user chose rather than to this app. The navy line comes first on
     purpose: a WebKit that cannot resolve `color-mix` or `AccentColor` drops the
     second declaration and keeps the navy, which is a readable result rather than
     a broken one. */
  .bar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.5rem;
    background: rgba(27, 29, 33, 0.35);
    background: color-mix(
      in srgb,
      var(--goat-accent) var(--goat-tint),
      transparent
    );
    backdrop-filter: blur(var(--goat-blur, 24px)) saturate(1.5);
    -webkit-backdrop-filter: blur(var(--goat-blur, 24px)) saturate(1.5);
    border-bottom: 1px solid rgba(255, 255, 255, 0.12);
  }

  h2 {
    margin: 0;
    font-size: 0.95rem;
  }

  .actions {
    display: flex;
    gap: 0.25rem;
  }

  .actions.trailing {
    margin-left: auto;
  }

  /* A drag region around the buttons leaves a press on any of them answered by
     the window manager instead of the button, so the empty space between the
     groups is the only part of the bar that drags. */
  .grip {
    flex: 1;
    align-self: stretch;
    min-width: 1rem;
    cursor: grab;
  }

  .scrim {
    position: fixed;
    inset: 0;
    z-index: 19;
    padding: 0;
    border: none;
    background: transparent;
    cursor: default;
  }

  .panel {
    position: absolute;
    top: 3rem;
    right: 0.75rem;
    z-index: 20;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    /* The bar is only 300px wide, so the panel is capped to the window instead
       of hanging off its right edge when a row wants more room than it has. */
    max-width: calc(100vw - 1rem);
    /* The panel hangs from a 48px bar, and a window fitted to a short capture
       can be shorter than the panel needs, so it takes only the room the bar
       leaves and scrolls the rest instead of running off the bottom edge. */
    max-height: calc(100vh - 3rem);
    overflow-y: auto;
    padding: 0.5rem;
    /* The panel is part of the same strip, so it takes the same accent tint and
       the same navy fallback as the bar rather than a colour of its own. */
    background: rgba(27, 29, 33, 0.35);
    background: color-mix(
      in srgb,
      var(--goat-accent) var(--goat-tint),
      transparent
    );
    backdrop-filter: blur(var(--goat-blur, 24px)) saturate(1.5);
    -webkit-backdrop-filter: blur(var(--goat-blur, 24px)) saturate(1.5);
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 0.5rem;
    box-shadow: 0 0.5rem 1.5rem rgba(0, 0, 0, 0.45);
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
  .switch {
    position: relative;
    width: 2.2rem;
    height: 1.15rem;
    margin-left: auto;
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

  /* The line under the switch is the outcome of the last press, so it is empty
     until there is one and it never carries the reason a press went wrong. */
  .switchline {
    margin: -0.15rem 0 0;
    color: #c9ccd2;
    font-size: 0.75rem;
  }

  .note {
    margin: 0;
    max-width: 16rem;
    color: #c9ccd2;
    font-size: 0.75rem;
    line-height: 1.35;
  }

  /* The body is the one place the window is a whole app, so it is the one place
     the desktop is allowed to show through: a wash far thinner than the bar and
     a blur that keeps the text legible over it. A fully transparent body would
     put white text straight onto whatever the user has behind, and an opaque one
     would throw away the reason the window was transparent to begin with. */
  .body {
    flex: 1;
    display: flex;
    flex-direction: column;
    box-sizing: border-box;
    padding: 1rem;
    background: rgba(255, 255, 255, 0.04);
    backdrop-filter: blur(var(--goat-blur, 18px));
    -webkit-backdrop-filter: blur(var(--goat-blur, 18px));
  }

  .status {
    display: inline-block;
    align-self: flex-start;
    margin: 0 0 0.75rem;
    padding: 0.25rem 0.6rem;
    background: rgba(0, 0, 0, 0.75);
    border-radius: 0.4rem;
  }

  .placeholder {
    display: inline-block;
    margin: 0.5rem 0 0;
    padding: 0.25rem 0.6rem;
    background: rgba(0, 0, 0, 0.75);
    border-radius: 0.4rem;
    font-size: 0.85rem;
  }

  .content {
    display: grid;
    grid-template-columns: 3fr 2fr;
    gap: 1rem;
  }

  .side {
    display: grid;
    grid-template-rows: 1fr 1fr;
    gap: 1rem;
  }

  /* Each text block gets its own plate of the same thin wash, so the desktop
     reads through the body of the window rather than only around its edges. The
     textareas inside stay as dark as they were, which is what keeps white text
     legible over a busy background. */
  .side section {
    padding: 0.5rem;
    background: rgba(255, 255, 255, 0.04);
    border-radius: 0.5rem;
  }

  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    margin-bottom: 0.4rem;
  }

  .shot canvas {
    width: 100%;
    height: auto;
    display: block;
    border: 1px solid rgba(255, 255, 255, 0.3);
    background: rgba(0, 0, 0, 0.3);
  }

  .shotwrap {
    position: relative;
    display: inline-block;
    max-width: 100%;
  }

  .shotwrap.armed {
    cursor: crosshair;
  }

  .shotwrap.armed canvas {
    pointer-events: none;
  }

  .overlay {
    position: fixed;
    inset: 0;
    cursor: crosshair;
    background: rgba(0, 0, 0, 0.15);
    z-index: 10;
  }

  .overlay-hint {
    position: fixed;
    top: 1rem;
    left: 50%;
    transform: translateX(-50%);
    margin: 0;
    padding: 0.4rem 0.8rem;
    background: rgba(0, 0, 0, 0.75);
    border-radius: 0.4rem;
    pointer-events: none;
  }

  .selrect.screen {
    position: fixed;
  }

  .selrect {
    position: absolute;
    border: 2px dashed #fff;
    background: rgba(255, 255, 255, 0.08);
    pointer-events: none;
  }

  section textarea {
    width: 100%;
    box-sizing: border-box;
    min-height: 5rem;
    padding: 0.5rem;
    background: rgba(0, 0, 0, 0.75);
    border: 1px solid rgba(255, 255, 255, 0.25);
    border-radius: 0.4rem;
    color: #fff;
    font: inherit;
    white-space: pre-wrap;
    word-break: break-word;
    resize: vertical;
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

  /* The close control sits where the title bar's own button would, so it is a
     bare glyph and the row it lives in is the only thing that gives it a shape
     on hover. */
  .icon.ghost {
    background: transparent;
    border: none;
  }

  .icon.ghost:hover {
    background: rgba(255, 255, 255, 0.1);
  }

  .icon.ghost:focus-visible {
    outline: 2px solid #9ec5ff;
    outline-offset: 2px;
  }
</style>
