<script>
  import { invoke } from '@tauri-apps/api/core';

  let response = "Waiting for ping...";

  async function doPing() {
    response = await invoke('ping');
  }
</script>

<main>
  <h1>GOaT IPC Test</h1>
  <button on:click={doPing}>Send Ping to Rust</button>
  <p>Result: {response}</p>
</main>
