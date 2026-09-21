<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { listen } from '@tauri-apps/api/event';
  import { getCurrentWindow } from '@tauri-apps/api/window';

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
  let imgSize = $state<{ width: number; height: number } | null>(null);

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
    imgSize = { width: result.image.width, height: result.image.height };
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
    if (!hasImage) {
      error = 'Capture a screenshot first, then drag on it to select a region';
      return;
    }
    error = '';
    selStart = null;
    selRect = null;
    selecting = true;
    window.addEventListener('keydown', cancelSelectOnEsc);
  }

  function stopSelectMode() {
    selecting = false;
    selStart = null;
    selRect = null;
    window.removeEventListener('keydown', cancelSelectOnEsc);
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
    const p = canvasPos(event);
    selStart = p;
    selRect = { x: p.x, y: p.y, w: 0, h: 0 };
  }

  function onSelMove(event: MouseEvent) {
    if (!selecting || !selStart || !canvasEl) return;
    const box = canvasEl.getBoundingClientRect();
    const cx = event.clientX - box.left;
    const cy = event.clientY - box.top;
    const x = Math.max(0, Math.min(selStart.x, cx));
    const y = Math.max(0, Math.min(selStart.y, cy));
    selRect = {
      x,
      y,
      w: Math.max(0, Math.min(cx, box.width) - x),
      h: Math.max(0, Math.min(cy, box.height) - y),
    };
  }

  async function onSelUp() {
    if (!selecting || !selRect || !canvasEl || !imgSize || busy) {
      return;
    }
    const box = canvasEl.getBoundingClientRect();
    const scaleX = imgSize.width / box.width;
    const scaleY = imgSize.height / box.height;
    const x = Math.max(0, Math.round(selRect.x * scaleX));
    const y = Math.max(0, Math.round(selRect.y * scaleY));
    const width = Math.max(1, Math.round(selRect.w * scaleX));
    const height = Math.max(1, Math.round(selRect.h * scaleY));
    const tooSmall = selRect.w < 4 || selRect.h < 4;
    stopSelectMode();
    if (tooSmall) {
      status = 'Selection too small';
      return;
    }
    busy = true;
    error = '';
    status = 'Reading selection...';
    try {
      const result = await invoke<ResultPayload>('ocr_selection', {
        x,
        y,
        width,
        height,
      });
      applyResult(result);
    } catch (e) {
      error = String(e);
      status = 'Selection failed';
    } finally {
      busy = false;
    }
  }

  function copy(text: string) {
    navigator.clipboard.writeText(text);
  }
</script>

<main>
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
      {#if selecting}
        <p class="placeholder">Drag on the screenshot, release to read — Esc to cancel.</p>
      {/if}
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
