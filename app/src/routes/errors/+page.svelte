<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { listen, type UnlistenFn } from '@tauri-apps/api/event';
  import { getCurrentWindow } from '@tauri-apps/api/window';

  type ErrorEntry = {
    time: string;
    source: string;
    message: string;
  };

  const HEADLINE = 'Errors and warnings';
  const EMPTY = 'No errors — everything is running clean.';
  /// The backend keeps a hundred lines, and the rows a window holds between
  /// records match it so the two lists never disagree about the oldest line.
  const MAX_ROWS = 100;

  let entries = $state<ErrorEntry[]>([]);
  const registered: UnlistenFn[] = [];

  /// A failure of the diagnostics window itself has no other window to report
  /// to, so it is shown in the list it was going to fill.
  function localEntry(message: string): ErrorEntry {
    return {
      time: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
      source: 'diagnostics',
      message,
    };
  }

  onMount(() => {
    invoke<ErrorEntry[]>('list_errors')
      .then((stored) => {
        entries = stored.toReversed();
      })
      .catch((e: unknown) => {
        entries = [localEntry(`The error log could not be read: ${String(e)}`)];
      });
    void listen<ErrorEntry>('app-error', (e) => {
      entries = [e.payload, ...entries].slice(0, MAX_ROWS);
    })
      .then((unlisten) => {
        registered.push(unlisten);
      })
      .catch((e: unknown) => {
        entries = [
          localEntry(`The live error feed could not be opened: ${String(e)}`),
          ...entries,
        ];
      });
    return () => {
      for (const unlisten of registered) unlisten();
    };
  });

  async function clear(): Promise<void> {
    try {
      await invoke('clear_errors');
      entries = [];
    } catch (e) {
      entries = [
        localEntry(`The error log could not be cleared: ${String(e)}`),
        ...entries,
      ];
    }
  }

  async function close(): Promise<void> {
    await getCurrentWindow().close();
  }
</script>

<main>
  <header>
    <h1>{HEADLINE}</h1>
    <div class="actions">
      <button onclick={clear} disabled={entries.length === 0}>Clear</button>
      <button onclick={close}>Close</button>
    </div>
  </header>

  {#if entries.length === 0}
    <p class="empty">{EMPTY}</p>
  {:else}
    <ul>
      {#each entries as entry}
        <li>
          <span class="time">{entry.time}</span>
          <span class="source">{entry.source}</span>
          <p class="message">{entry.message}</p>
        </li>
      {/each}
    </ul>
  {/if}
</main>

<style>
  :global(html),
  :global(body) {
    margin: 0;
    background: #1b1d21;
    color: #fff;
    font-family: system-ui, sans-serif;
  }

  main {
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    height: 100vh;
    padding: 1rem;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
  }

  h1 {
    margin: 0;
    font-size: 1.1rem;
  }

  .actions {
    display: flex;
    gap: 0.5rem;
  }

  .empty {
    margin: 0;
    padding: 0.75rem 0.9rem;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 0.4rem;
    color: #c9ccd2;
    font-size: 0.9rem;
  }

  ul {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  li {
    display: grid;
    grid-template-columns: auto auto 1fr;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.4rem 0;
    border-top: 1px solid rgba(255, 255, 255, 0.12);
    font-size: 0.85rem;
  }

  .time {
    color: #9aa0aa;
    font-variant-numeric: tabular-nums;
  }

  .source {
    padding: 0.1rem 0.4rem;
    background: rgba(0, 0, 0, 0.5);
    border-radius: 0.3rem;
    color: #7fd1ff;
  }

  .message {
    margin: 0;
    color: #ff9d9d;
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

  button:disabled {
    opacity: 0.4;
    cursor: default;
  }
</style>
