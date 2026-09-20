<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { listen } from '@tauri-apps/api/event';

  type CapturedImage = {
    width: number;
    height: number;
    rgba: number[];
  };

  type ResultPayload = {
    image: CapturedImage;
    ocr_text: string;
    translated_text: string;
  };

  type ModelsStatus = {
    detection: boolean;
    recognition: boolean;
    keys: boolean;
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
    status = ocrText.trim() ? 'Result ready' : 'No text detected';
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
  </div>
</main>

<style>
  :global(body) {
    margin: 0;
    padding: 0;
    background: transparent;
    font-family: system-ui, sans-serif;
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
    max-width: 100%;
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
