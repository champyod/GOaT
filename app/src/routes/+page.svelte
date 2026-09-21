<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { listen } from '@tauri-apps/api/event';
  import { getCurrentWindow, PhysicalPosition } from '@tauri-apps/api/window';

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
    error: string;
  };

  type ModelsStatus = {
    ocr: boolean;
    nllb: boolean;
    ready: boolean;
  };

  let canvasEl: HTMLCanvasElement | undefined = $state();
  let status = $state('Waiting for hotkey...');
  let ocrText = $state('');
  let translatedText = $state('');
  let autostart = $state(false);
  let busy = $state(false);
  let error = $state('');
  let hotkey = $state('Ctrl+Shift+S');
  let newHotkey = $state('Ctrl+Shift+S');
  let hotkeyError = $state('');
  let hasImage = $state(false);
  let isFullscreen = $state(false);
  let monitors = $state<MonitorInfo[]>([]);
  let monitor = $state(0);
  let selecting = $state(false);
  let selStart = $state<{ x: number; y: number } | null>(null);
  let selRect = $state<{ x: number; y: number; w: number; h: number } | null>(
    null
  );
  let selOrigin = $state<{ x: number; y: number } | null>(null);
  let selScale = $state(1);

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

  function applyResult(result: ResultPayload) {
    draw(result.image);
    ocrText = result.ocr_text;
    translatedText = result.translated_text;
    error = result.error;
    if (error) {
      status = 'Capture incomplete';
    } else {
      status = ocrText.trim() ? 'Result ready' : 'No text detected';
    }
  }

  async function capture() {
    if (busy) return;
    busy = true;
    error = '';
    status = 'Capturing...';
    try {
      const result = await invoke<ResultPayload>('capture_primary');
      applyResult(result);
    } catch (e) {
      error = String(e);
      status = 'Capture failed';
    } finally {
      busy = false;
    }
  }

  onMount(() => {
    const unlisten = listen<ResultPayload>('capture-result', (event) => {
      applyResult(event.payload);
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
        error = String(e);
      });
    invoke<boolean>('is_autostart')
      .then((value) => {
        autostart = value;
      })
      .catch((e) => {
        error = String(e);
      });
    invoke<string>('get_hotkey')
      .then((value) => {
        hotkey = value;
        newHotkey = value;
      })
      .catch((e) => {
        error = String(e);
      });
    invoke<MonitorInfo[]>('list_monitors')
      .then((value) => {
        monitors = value;
      })
      .catch((e) => {
        error = String(e);
      });
    invoke<number>('get_monitor')
      .then((value) => {
        monitor = value;
      })
      .catch((e) => {
        error = String(e);
      });
    return () => {
      unlisten.then((f) => f());
    };
  });

  async function close() {
    await invoke('hide_window');
  }

  async function toggleAutostart() {
    autostart = await invoke<boolean>('set_autostart', {
      enabled: !autostart,
    });
  }

  async function saveHotkey() {
    hotkeyError = '';
    try {
      hotkey = await invoke<string>('set_hotkey', { hotkey: newHotkey });
    } catch (e) {
      hotkeyError = String(e);
    }
  }

  async function saveMonitor(event: Event) {
    const select = event.currentTarget as HTMLSelectElement;
    try {
      monitor = await invoke<number>('set_monitor', {
        monitor: Number(select.value),
      });
    } catch (e) {
      error = String(e);
    }
  }

  async function toggleFullscreen() {
    const win = getCurrentWindow();
    const next = !(await win.isFullscreen());
    await win.setFullscreen(next);
    isFullscreen = next;
  }

  async function startSelect() {
    const win = getCurrentWindow();
    const mon = monitors.find((m) => m.index === monitor) ?? monitors[0];
    if (!mon) {
      error = 'No monitor info available';
      return;
    }
    error = '';
    if (isFullscreen) {
      await win.setFullscreen(false);
      isFullscreen = false;
    }
    await win.setPosition(new PhysicalPosition(mon.x, mon.y));
    await win.setFullscreen(true);
    isFullscreen = true;
    selOrigin = { x: mon.x, y: mon.y };
    selScale = await win.scaleFactor();
    selStart = null;
    selRect = null;
    selecting = true;
    window.addEventListener('keydown', cancelSelectOnEsc);
  }

  async function stopSelectMode() {
    selecting = false;
    selStart = null;
    selRect = null;
    window.removeEventListener('keydown', cancelSelectOnEsc);
    const win = getCurrentWindow();
    if (await win.isFullscreen()) {
      await win.setFullscreen(false);
      isFullscreen = false;
    }
  }

  function cancelSelectOnEsc(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      stopSelectMode();
    }
  }

  function onSelDown(event: MouseEvent) {
    selStart = { x: event.clientX, y: event.clientY };
    selRect = { x: event.clientX, y: event.clientY, w: 0, h: 0 };
  }

  function onSelMove(event: MouseEvent) {
    if (!selStart) return;
    const x = Math.min(selStart.x, event.clientX);
    const y = Math.min(selStart.y, event.clientY);
    selRect = {
      x,
      y,
      w: Math.abs(event.clientX - selStart.x),
      h: Math.abs(event.clientY - selStart.y),
    };
  }

  async function onSelUp() {
    if (!selRect || !selOrigin || busy) {
      if (!busy) await stopSelectMode();
      return;
    }
    const x = Math.max(
      0,
      Math.round(selRect.x * selScale + selOrigin.x)
    );
    const y = Math.max(
      0,
      Math.round(selRect.y * selScale + selOrigin.y)
    );
    const width = Math.max(1, Math.round(selRect.w * selScale));
    const height = Math.max(1, Math.round(selRect.h * selScale));
    const tooSmall = selRect.w < 4 || selRect.h < 4;
    await stopSelectMode();
    if (tooSmall) {
      status = 'Selection too small';
      return;
    }
    busy = true;
    error = '';
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
      error = String(e);
      status = 'Capture failed';
    } finally {
      busy = false;
    }
  }

  function copy(text: string) {
    navigator.clipboard.writeText(text);
  }
</script>

<main>
  {#if selecting}
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions --
      Full-screen drag surface; Esc to cancel is handled on window keydown. -->
    <div
      class="overlay"
      role="application"
      aria-label="Drag to select a screen region"
      onmousedown={onSelDown}
      onmousemove={onSelMove}
      onmouseup={onSelUp}
    >
      <p class="overlay-hint">Drag to select a region — Esc to cancel</p>
      {#if selRect}
        <div
          class="selrect"
          style="left: {selRect.x}px; top: {selRect.y}px; width: {selRect.w}px; height: {selRect.h}px;"
        ></div>
      {/if}
    </div>
  {:else}
  <div class="toolbar" data-tauri-drag-region>
    <h1 data-tauri-drag-region>GOaT</h1>
    <span class="hotkey-hint" data-tauri-drag-region>{hotkey}</span>
    <label>
      <input type="checkbox" checked={autostart} onclick={toggleAutostart} />
      Start at login
    </label>
    <button onclick={capture} disabled={busy}>
      {busy ? 'Working...' : 'Capture'}
    </button>
    <button onclick={startSelect} disabled={busy}>Select region</button>
    <button onclick={toggleFullscreen}>
      {isFullscreen ? 'Unfullscreen' : 'Fullscreen'}
    </button>
    <button onclick={close}>Close (hide)</button>
  </div>

  <p class="status">{status}</p>
  {#if error}
    <p class="error">{error}</p>
  {/if}

  <div class="content">
    <section class="shot">
      <h2>Screenshot</h2>
      {#if !hasImage}
        <p class="placeholder">No screenshot yet — press {hotkey} or Capture.</p>
      {/if}
      <canvas bind:this={canvasEl}></canvas>
    </section>

    <div class="side">
      <section>
        <h2>OCR'ed text</h2>
        <textarea
          bind:value={ocrText}
          placeholder="No text yet."
          rows={6}
        ></textarea>
        <button onclick={() => copy(ocrText)} disabled={!ocrText}>Copy</button>
      </section>
      <section>
        <h2>Translated text</h2>
        <textarea
          bind:value={translatedText}
          placeholder="No translation yet."
          rows={6}
        ></textarea>
        <button onclick={() => copy(translatedText)} disabled={!translatedText}>
          Copy
        </button>
      </section>
    </div>
  </div>

  <div class="settings">
    <label>
      Hotkey
      <input bind:value={newHotkey} placeholder="Ctrl+Shift+S" />
    </label>
    <button onclick={saveHotkey}>Save hotkey</button>
    {#if hotkeyError}
      <span class="error">{hotkeyError}</span>
    {/if}
    <label>
      Monitor
      <select value={monitor} onchange={saveMonitor}>
        {#each monitors as m}
          <option value={m.index}>
            {m.name}{m.is_primary ? ' (primary)' : ''} — {m.width}x{m.height}
          </option>
        {/each}
      </select>
    </label>
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
    min-height: 100vh;
    padding: 1rem;
    box-sizing: border-box;
    color: #fff;
  }

  .toolbar {
    display: flex;
    align-items: center;
    gap: 1rem;
  }

  .error {
    color: #ff9d9d;
  }

  .hotkey-hint {
    opacity: 0.7;
    font-size: 0.85rem;
  }

  .placeholder {
    opacity: 0.6;
    font-size: 0.85rem;
  }

  h1 {
    margin: 0;
    font-size: 1.2rem;
  }

  h2 {
    margin: 0 0 0.4rem;
    font-size: 0.95rem;
  }

  .status {
    opacity: 0.7;
  }

  .content {
    display: grid;
    grid-template-columns: 3fr 2fr;
    gap: 1rem;
    margin-top: 1rem;
  }

  .shot canvas {
    width: 100%;
    height: auto;
    display: block;
    border: 1px solid rgba(255, 255, 255, 0.3);
    background: rgba(0, 0, 0, 0.3);
  }

  .side {
    display: grid;
    grid-template-rows: 1fr 1fr;
    gap: 1rem;
  }

  .settings {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-top: 1rem;
  }

  .settings input {
    background: rgba(255, 255, 255, 0.12);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 0.4rem;
    padding: 0.3rem 0.6rem;
  }

  .settings select {
    background: rgba(255, 255, 255, 0.12);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 0.4rem;
    padding: 0.3rem 0.6rem;
  }

  .settings select option {
    color: #000;
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
    background: rgba(0, 0, 0, 0.6);
    border-radius: 0.4rem;
    pointer-events: none;
  }

  .selrect {
    position: fixed;
    border: 2px dashed #fff;
    background: rgba(255, 255, 255, 0.08);
    pointer-events: none;
  }

  section textarea {
    width: 100%;
    box-sizing: border-box;
    min-height: 5rem;
    padding: 0.5rem;
    background: rgba(255, 255, 255, 0.12);
    border: 1px solid transparent;
    border-radius: 0.4rem;
    color: #fff;
    font: inherit;
    white-space: pre-wrap;
    word-break: break-word;
    resize: vertical;
  }

  button {
    background: rgba(255, 255, 255, 0.2);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 0.4rem;
    padding: 0.3rem 0.8rem;
    cursor: pointer;
  }

  button:hover {
    background: rgba(255, 255, 255, 0.3);
  }

  button:disabled {
    opacity: 0.4;
    cursor: default;
  }
</style>
