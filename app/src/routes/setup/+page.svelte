<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { listen, type UnlistenFn } from '@tauri-apps/api/event';
  import { getCurrentWindow } from '@tauri-apps/api/window';
  import Keyboard from '@lucide/svelte/icons/keyboard';

  const ICON_SIZE = 16;
  const CAPTURE_TRIGGER_TITLE = 'Choose capture trigger';
  const REGION_TRIGGER_TITLE = 'Choose region trigger';
  const WAITING_TITLE = 'Waiting...';

  type HotkeyStatus = {
    backend: 'system' | 'portal';
    detail: string;
    warning: string;
  };

  /// A portal session fires the trigger its own dialog produced, so nothing typed
  /// here is bound and a failed save has nowhere else to go.
  const PORTAL_TRIGGER_NOTE =
    'No typed shortcut can be saved here — the desktop owns triggers on this session. On KDE Plasma: System Settings → Keyboard → Shortcuts. On GNOME: Settings → Keyboard → View and Customize Shortcuts.';

  let newHotkey = $state('Ctrl+Shift+S');
  let newSelectHotkey = $state('Ctrl+Shift+E');
  let hotkeyStatus = $state<HotkeyStatus | null>(null);
  let status = $state('');
  let saveError = $state('');
  let savingHotkey = $state(false);
  let dontShow = $state(false);
  const registered: UnlistenFn[] = [];

  // A portal session fires only the trigger its own dialog produced, so the saved
  // value is not what is bound and the row shows it as read-only text.
  const portalBackend = $derived(hotkeyStatus?.backend === 'portal');
  // The backend keeps the warning empty while every trigger still carries a key,
  // so a filled one is the only case that has to interrupt the steps.
  const triggerUnbound = $derived(Boolean(hotkeyStatus?.warning));

  /// The diagnostics window is the one place a problem is written down, so a
  /// failure here is handed to it as well as kept on this page, where the reason
  /// can be read next to what it means. The backend already records a shortcut it
  /// could not bind, so its reason is written down once rather than twice.
  function reportError(source: string, message: string): void {
    void invoke('report_frontend_error', { source, message }).catch(
      (e: unknown) => {
        console.error(`GOaT could not record: ${message}`, e);
      }
    );
  }

  /// A save that failed leaves nothing bound, so the failure is kept where the
  /// steps are rather than only in a window that may not be open.
  function showSaveFailure(error: unknown): void {
    const message = String(error);
    saveError = message;
    reportError('hotkey', message);
  }

  onMount(() => {
    invoke<string>('get_hotkey')
      .then((value) => {
        newHotkey = value;
      })
      .catch((e) => {
        reportError('hotkey', String(e));
      });
    invoke<string>('get_select_hotkey')
      .then((value) => {
        newSelectHotkey = value;
      })
      .catch((e) => {
        reportError('hotkey', String(e));
      });
    invoke<boolean>('get_hide_bind_notice')
      .then((value) => {
        dontShow = value;
      })
      .catch((e) => {
        reportError('settings', String(e));
      });
    invoke<HotkeyStatus>('hotkey_status')
      .then((value) => {
        hotkeyStatus = value;
      })
      .catch((e) => {
        reportError('hotkey', String(e));
      });
    void listen<HotkeyStatus>('hotkey-status', (e) => {
      hotkeyStatus = e.payload;
    })
      .then((unlisten) => {
        registered.push(unlisten);
      })
      .catch((e) => {
        reportError('event', `the hotkey-status feed could not be opened: ${String(e)}`);
      });
    return () => {
      for (const unlisten of registered) unlisten();
    };
  });

  async function saveHotkey(): Promise<void> {
    if (savingHotkey) return;
    savingHotkey = true;
    try {
      // The backend hands back the shortcut it actually holds, which on a portal
      // is not the one typed, so the field follows it instead of the keystrokes.
      newHotkey = await invoke<string>('set_hotkey', { hotkey: newHotkey });
      saveError = '';
      status = 'Capture trigger saved';
    } catch (e) {
      showSaveFailure(e);
    } finally {
      savingHotkey = false;
    }
  }

  async function saveSelectHotkey(): Promise<void> {
    if (savingHotkey) return;
    savingHotkey = true;
    try {
      newSelectHotkey = await invoke<string>('set_select_hotkey', {
        hotkey: newSelectHotkey,
      });
      saveError = '';
      status = 'Region trigger saved';
    } catch (e) {
      showSaveFailure(e);
    } finally {
      savingHotkey = false;
    }
  }

  async function close(): Promise<void> {
    try {
      await getCurrentWindow().close();
    } catch (e) {
      reportError('window', String(e));
    }
  }

  function onDismissChange(event: Event): void {
    const box = event.currentTarget as HTMLInputElement;
    void setDontShow(box.checked);
  }

  async function setDontShow(hide: boolean): Promise<void> {
    try {
      dontShow = await invoke<boolean>('set_hide_bind_notice', { hide });
    } catch (e) {
      reportError('settings', String(e));
      return;
    }
    if (dontShow) await close();
  }
</script>

<main>
  <h1>Set a keyboard shortcut</h1>

  {#if triggerUnbound}
    <p class="banner unbound" role="alert">No shortcut bound yet.</p>
  {/if}

  <ol class="steps">
    <li>
      <div class="row">
        {#if portalBackend}
          <span class="rowlabel">
            Capture
            <span class="bound">{newHotkey}</span>
          </span>
        {:else}
          <label class="rowlabel">
            Capture
            <input bind:value={newHotkey} placeholder="Ctrl+Shift+S" />
          </label>
        {/if}
        <button
          class="icon"
          class:busy={savingHotkey}
          aria-label={CAPTURE_TRIGGER_TITLE}
          title={savingHotkey ? WAITING_TITLE : CAPTURE_TRIGGER_TITLE}
          onclick={saveHotkey}
          disabled={savingHotkey}
        >
          <Keyboard size={ICON_SIZE} />
        </button>
      </div>
    </li>
    <li>
      <div class="row">
        {#if portalBackend}
          <span class="rowlabel">
            Region
            <span class="bound">{newSelectHotkey}</span>
          </span>
        {:else}
          <label class="rowlabel">
            Region
            <input bind:value={newSelectHotkey} placeholder="Ctrl+Shift+E" />
          </label>
        {/if}
        <button
          class="icon"
          class:busy={savingHotkey}
          aria-label={REGION_TRIGGER_TITLE}
          title={savingHotkey ? WAITING_TITLE : REGION_TRIGGER_TITLE}
          onclick={saveSelectHotkey}
          disabled={savingHotkey}
        >
          <Keyboard size={ICON_SIZE} />
        </button>
      </div>
    </li>
    <li>
      <p class="step">Press your capture shortcut to grab the screen.</p>
    </li>
  </ol>

  {#if status}
    <p class="banner" role="status">{status}</p>
  {/if}

  {#if saveError}
    <p class="banner failure" role="alert">
      {saveError}
      {#if portalBackend}
        <span class="hint">{PORTAL_TRIGGER_NOTE}</span>
      {/if}
    </p>
  {/if}

  <div class="dismiss">
    <label>
      <input type="checkbox" checked={dontShow} onchange={onDismissChange} />
      Don't show this again
    </label>
    <button onclick={close}>Close</button>
  </div>

  <details>
    <summary>Why am I seeing this?</summary>
    <div class="explain">
      <p>
        GOaT waits for a trigger before it captures anything, and it reads the
        shortcut from whichever backend your session gives it.
      </p>
      {#if portalBackend}
        <p>
          These triggers are set in your desktop's own shortcut settings, not in
          GOaT. The buttons open that dialog.
        </p>
      {/if}
      {#if hotkeyStatus}
        <p class="hotkeyline" data-backend={hotkeyStatus.backend}>
          {hotkeyStatus.detail}
        </p>
        {#if hotkeyStatus.warning}
          <p class="warning">{hotkeyStatus.warning}</p>
        {/if}
      {/if}
    </div>
  </details>
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
    gap: 0.4rem;
    padding: 0.9rem 1rem;
  }

  h1 {
    margin: 0;
    font-size: 1.05rem;
  }

  .steps {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    margin: 0.2rem 0 0;
    padding-left: 1.3rem;
  }

  .step {
    margin: 0;
    font-size: 0.8rem;
    color: #c9ccd2;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .rowlabel {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-width: 0;
    font-size: 0.85rem;
  }

  .row input {
    min-width: 0;
    font-size: 0.85rem;
    background: rgba(255, 255, 255, 0.12);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.4);
    border-radius: 0.4rem;
    padding: 0.25rem 0.5rem;
  }

  .bound {
    color: #c9ccd2;
    font-size: 0.85rem;
  }

  .row .icon {
    margin-left: auto;
  }

  .banner {
    display: inline-block;
    align-self: flex-start;
    margin: 0;
    padding: 0.25rem 0.6rem;
    background: rgba(0, 0, 0, 0.45);
    border-radius: 0.4rem;
    font-size: 0.85rem;
  }

  .unbound {
    color: #ffc46b;
  }

  .failure {
    border-left: 3px solid #ff9d9d;
  }

  .hint {
    display: block;
    margin-top: 0.3rem;
    color: #c9ccd2;
  }

  .warning {
    margin: 0;
    color: #ffc46b;
  }

  .hotkeyline {
    align-self: flex-start;
    margin: 0;
    padding: 0.25rem 0.6rem;
    background: rgba(0, 0, 0, 0.45);
    border-left: 3px solid #7fd1ff;
    border-radius: 0.4rem;
    font-size: 0.85rem;
  }

  .hotkeyline[data-backend='portal'] {
    border-left-color: #ffc46b;
  }

  details {
    margin-top: 0.6rem;
    padding-top: 0.5rem;
    border-top: 1px solid rgba(255, 255, 255, 0.2);
    font-size: 0.8rem;
    color: #c9ccd2;
  }

  summary {
    cursor: pointer;
  }

  .explain {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .explain p {
    margin: 0;
  }

  .dismiss {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    margin-top: 0.5rem;
    font-size: 0.85rem;
  }

  button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
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

  .icon {
    width: 2rem;
    height: 2rem;
    padding: 0;
  }

  .icon.busy {
    animation: busy-pulse 1.1s ease-in-out infinite;
  }

  @keyframes busy-pulse {
    0%,
    100% {
      opacity: 0.85;
    }
    50% {
      opacity: 0.3;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .icon.busy {
      animation: none;
      opacity: 0.4;
    }
  }
</style>
