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
  const MENU_HEIGHT = 320;
  const BODY_WIDTH = 800;
  const BODY_HEIGHT = 600;
  const NOT_BOUND = 'Not bound';
  const CAPTURE_TRIGGER_TITLE = 'Save capture trigger';
  const REGION_TRIGGER_TITLE = 'Save region trigger';
  const CHOOSE_CAPTURE_TITLE = 'Choose capture trigger';
  const CHOOSE_REGION_TITLE = 'Choose region trigger';
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
    <div class="body">
      <p class="status" role="status">{status}</p>

      <div class="content">
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
    background: rgba(23, 52, 102, 0.62);
    background: color-mix(in srgb, AccentColor 62%, transparent);
    backdrop-filter: blur(14px) saturate(1.5);
    -webkit-backdrop-filter: blur(14px) saturate(1.5);
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
    padding: 0.5rem;
    /* The panel is part of the same strip, so it takes the same accent tint and
       the same navy fallback as the bar rather than a colour of its own. */
    background: rgba(23, 52, 102, 0.62);
    background: color-mix(in srgb, AccentColor 62%, transparent);
    backdrop-filter: blur(14px) saturate(1.5);
    -webkit-backdrop-filter: blur(14px) saturate(1.5);
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
    backdrop-filter: blur(18px);
    -webkit-backdrop-filter: blur(18px);
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
