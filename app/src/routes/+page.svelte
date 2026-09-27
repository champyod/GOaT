<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { listen, type EventName, type UnlistenFn } from '@tauri-apps/api/event';
  import { getCurrentWindow } from '@tauri-apps/api/window';
  import Camera from '@lucide/svelte/icons/camera';
  import ScanLine from '@lucide/svelte/icons/scan-line';
  import Menu from '@lucide/svelte/icons/menu';
  import CopyIcon from '@jis3r/icons/icons/copy';
  import CheckIcon from '@jis3r/icons/icons/check';

  const COPIED_FEEDBACK_MS = 1600;
  const ICON_SIZE = 18;

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

  let canvasEl: HTMLCanvasElement | undefined = $state();
  let status = $state('Ready');
  let ocrText = $state('');
  let translatedText = $state('');
  let autostart = $state(false);
  let busy = $state(false);
  let hasImage = $state(false);
  let monitors = $state<MonitorInfo[]>([]);
  let monitor = $state(0);
  let selecting = $state(false);
  let screenMode = $state(false);
  let menuOpen = $state(false);
  let copiedOcr = $state(false);
  let copiedTranslated = $state(false);
  let selStart = $state<{ x: number; y: number } | null>(null);
  let selRect = $state<{ x: number; y: number; w: number; h: number } | null>(
    null
  );
  let imgSize = $state<{ width: number; height: number } | null>(null);
  let selOffset = $state({ x: 0, y: 0 });
  let copiedTimer: ReturnType<typeof setTimeout> | undefined;
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

  function applyResult(
    result: ResultPayload,
    origin: { x: number; y: number } | null = null
  ) {
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

  function drawImage(image: CapturedImage) {
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
    return () => {
      for (const unlisten of registered) unlisten();
      if (copiedTimer) clearTimeout(copiedTimer);
    };
  });

  function onAutostartChange(event: Event): void {
    const box = event.currentTarget as HTMLInputElement;
    void setAutostart(box.checked);
  }

  async function setAutostart(enabled: boolean): Promise<void> {
    try {
      autostart = await invoke<boolean>('set_autostart', { enabled });
    } catch (e) {
      reportError('autostart', String(e));
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

  function toggleMenu(): void {
    menuOpen = !menuOpen;
  }

  function closeMenu(): void {
    menuOpen = false;
  }

  /// The shortcut steps are only written down in the setup window, so this
  /// raises it and closes the panel that asked for it.
  async function openSetup(): Promise<void> {
    closeMenu();
    try {
      await invoke('show_setup');
    } catch (e) {
      reportError('window', String(e));
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

<main>
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
      <h1>GOaT</h1>
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
          disabled={busy}
          aria-label="Select region"
          title="Select region"
        >
          <ScanLine size={ICON_SIZE} />
        </button>
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
    </div>

    {#if menuOpen}
      <button
        class="scrim"
        tabindex="-1"
        aria-label="Close menu"
        onclick={closeMenu}
      ></button>
      <div class="panel">
        <label class="row">
          <input
            type="checkbox"
            checked={autostart}
            onchange={onAutostartChange}
          />
          Start at login
        </label>
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
        <button class="row action" onclick={openSetup}>
          Keyboard shortcuts…
        </button>
      </div>
    {/if}

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
</main>

<style>
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
    min-height: 100vh;
    color: #fff;
  }

  /* The native title bar carries the window controls, so this strip is opaque
     and stays a fixed-height row no matter how wide the window gets. */
  .bar {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.5rem 0.75rem;
    background: #1b1d21;
    border-bottom: 1px solid rgba(255, 255, 255, 0.12);
  }

  h1 {
    margin: 0;
    font-size: 1.1rem;
  }

  h2 {
    margin: 0;
    font-size: 0.95rem;
  }

  .actions {
    display: flex;
    gap: 0.35rem;
    margin-left: auto;
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
    gap: 0.5rem;
    padding: 0.6rem;
    background: #1b1d21;
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 0.5rem;
    box-shadow: 0 0.5rem 1.5rem rgba(0, 0, 0, 0.45);
  }

  .row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.85rem;
    white-space: nowrap;
  }

  .row select {
    background: rgba(255, 255, 255, 0.12);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 0.4rem;
    padding: 0.3rem 0.6rem;
  }

  .row select option {
    color: #000;
  }

  .row.action {
    justify-content: center;
    padding: 0.35rem 0.8rem;
    background: rgba(255, 255, 255, 0.12);
    border: 1px solid rgba(255, 255, 255, 0.3);
    border-radius: 0.4rem;
    cursor: pointer;
  }

  .row.action:hover {
    background: rgba(255, 255, 255, 0.22);
  }

  .body {
    flex: 1;
    display: flex;
    flex-direction: column;
    box-sizing: border-box;
    padding: 1rem;
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
    width: 2rem;
    height: 2rem;
    padding: 0;
    background: rgba(255, 255, 255, 0.08);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 0.45rem;
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
