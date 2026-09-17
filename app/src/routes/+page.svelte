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

  let canvasEl: HTMLCanvasElement | undefined = $state();
  let status = $state('Waiting for hotkey...');
  let ocrText = $state('');
  let translatedText = $state('');
  let autostart = $state(false);

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
  }

  async function capture() {
    status = 'Capturing...';
    try {
      const result = await invoke<ResultPayload>('capture_primary');
      draw(result.image);
      ocrText = result.ocr_text;
      translatedText = result.translated_text;
      status = 'Result ready';
    } catch (e) {
      status = String(e);
    }
  }

  onMount(() => {
    const unlisten = listen<ResultPayload>('capture-result', (event) => {
      draw(event.payload.image);
      ocrText = event.payload.ocr_text;
      translatedText = event.payload.translated_text;
      status = 'Result ready';
    });
    invoke<boolean>('is_autostart').then((value) => {
      autostart = value;
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

  function copy(text: string) {
    navigator.clipboard.writeText(text);
  }
</script>

<main>
  <div class="toolbar">
    <h1>GOaT</h1>
    <label>
      <input type="checkbox" checked={autostart} onclick={toggleAutostart} />
      Start at login
    </label>
    <button onclick={capture}>Capture</button>
    <button onclick={close}>Close (hide)</button>
  </div>

  <p class="status">{status}</p>

  <canvas bind:this={canvasEl}></canvas>

  <div class="results">
    <section>
      <h2>OCR'ed text</h2>
      <p>{ocrText}</p>
      <button onclick={() => copy(ocrText)}>Copy</button>
    </section>
    <section>
      <h2>Translated text</h2>
      <p>{translatedText}</p>
      <button onclick={() => copy(translatedText)}>Copy</button>
    </section>
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

  canvas {
    max-width: 100%;
    border: 1px solid rgba(255, 255, 255, 0.3);
    background: rgba(0, 0, 0, 0.3);
  }

  .results {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin-top: 1rem;
  }

  section p {
    min-height: 5rem;
    padding: 0.5rem;
    background: rgba(255, 255, 255, 0.12);
    border-radius: 0.4rem;
    white-space: pre-wrap;
    word-break: break-word;
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
</style>
