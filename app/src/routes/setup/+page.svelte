<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { listen, type UnlistenFn } from '@tauri-apps/api/event';
  import { getCurrentWindow } from '@tauri-apps/api/window';
  import Keyboard from '@lucide/svelte/icons/keyboard';
  import Check from '@lucide/svelte/icons/check';
  import { CheckFlowbite, HourglassFlowbite, KeyboardFlowbite } from 'svelte-animated-icons';

  const ICON_SIZE = 16;
  const CAPTURE_TRIGGER_TITLE = 'Choose capture trigger';
  const REGION_TRIGGER_TITLE = 'Choose region trigger';
  // A button that is off because the desktop has no dialog to open has to say so
  // where the pointer is, and the guidance under it is what the title points at.
  const NO_DIALOG_TITLE = 'This desktop has no shortcut dialog — see below';
  // The guidance the two buttons point at when they are off, and the element the
  // description of a disabled button names.
  const GUIDANCE_ID = 'no-shortcut-dialog-guidance';
  const WAITING_TITLE = 'Waiting...';
  const DIALOG_WAIT_HINT = 'Desktop dialog open — pick keys there.';
  const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';
  const CAPTURE_PLACEHOLDER = 'Ctrl+Shift+S';
  const REGION_PLACEHOLDER = 'Ctrl+Shift+E';
  const CAPTURE_STEP_HINT = 'Press your capture shortcut to grab the screen.';
  const UNBOUND_HINT = 'Once bound, press it to grab the screen.';
  const NOT_BOUND = 'Not bound';
  const UNBOUND_NOTE = 'No shortcut bound yet.';
  const FLASH_MS = 900;

  type HotkeyStatus = {
    backend: 'system' | 'portal';
    detail: string;
    warning: string;
    // The trigger each shortcut is actually bound to, or null when the backend
    // has bound none. The rows must read these, never the saved values: on a
    // portal the saved shortcut is not what fires.
    capture_trigger: string | null;
    select_trigger: string | null;
    // Whether the desktop can be asked for its shortcut dialog. A portal that
    // implements an interface old enough to predate that dialog cannot be, so
    // there is nothing behind a Choose button there and the guidance replaces
    // it. A window-system session never asks, and is never held to it. Null is
    // not a "no": it is a session that has not reported yet, which keeps the
    // buttons live rather than switching them off on a desktop that has one.
    configure_supported: boolean | null;
  };

  type TriggerRow = 'capture' | 'region';
  type StatusTone = 'info' | 'warning' | 'error';
  type IconState = 'idle' | 'waiting' | 'flash';

  const TRIGGER_COMMANDS: Record<TriggerRow, { read: string; write: string; saved: string }> = {
    capture: { read: 'get_hotkey', write: 'set_hotkey', saved: 'Capture trigger saved' },
    region: { read: 'get_select_hotkey', write: 'set_select_hotkey', saved: 'Region trigger saved' },
  };

  // The portal names the shortcut it is reconfiguring, so the waiting state lands
  // on the row that asked for it rather than on both. These mirror the ids the
  // backend holds in `binding::CAPTURE_ID` and `binding::SELECT_ID`.
  const ROWS_BY_ID: Record<string, TriggerRow> = {
    goat_capture: 'capture',
    goat_region_select: 'region',
  };

  let newHotkey = $state(CAPTURE_PLACEHOLDER);
  let newSelectHotkey = $state(REGION_PLACEHOLDER);
  let hotkeyStatus = $state<HotkeyStatus | null>(null);
  let statusNote = $state('');
  let statusTone = $state<StatusTone>('info');
  let savingHotkey = $state(false);
  // The desktop's own dialog owns the round from the moment it opens, so the
  // button reports a wait the user can see somewhere else instead of a call that
  // is still in flight here.
  let waitingChoice = $state(false);
  let waitingRow = $state<TriggerRow | null>(null);
  let flashRow = $state<TriggerRow | null>(null);
  let reducedMotion = $state(false);
  let dontShow = $state(false);
  // The line for a desktop with no dialog. The bind that would have produced it
  // is the very button that is disabled, so it is asked for on its own, and only
  // once: the verdict comes from the backend and does not change mid-session.
  let guidanceText = $state('');
  let guidanceAsked = false;
  let flashTimer: ReturnType<typeof setTimeout> | null = null;
  const registered: UnlistenFn[] = [];

  // A portal session fires only the trigger its own dialog produced, so the saved
  // value is not what is bound and the row shows it as read-only text.
  const portalBackend = $derived(hotkeyStatus?.backend === 'portal');
  // The rows show what the backend actually holds, so "unbound" means a trigger
  // is genuinely missing — never just that a warning exists. A warning with both
  // triggers present (e.g. a stale-release note after a good remap) is not
  // "No shortcut bound yet."
  const captureTrigger = $derived(hotkeyStatus?.capture_trigger ?? null);
  const selectTrigger = $derived(hotkeyStatus?.select_trigger ?? null);
  const triggerUnbound = $derived(
    portalBackend
      ? captureTrigger === null || selectTrigger === null
      : Boolean(hotkeyStatus?.warning)
  );
  const stepHint = $derived(captureTrigger ? CAPTURE_STEP_HINT : UNBOUND_HINT);
  // A portal that reports an interface old enough to predate the dialog has none
  // to open, so the buttons that open it are off from the moment the backend says
  // so rather than failing on a press. Only a "no" turns them off: a session that
  // has not reported yet is left alone, because a desktop that does have a dialog
  // would then be showing a button that cannot work. A window-system session binds
  // what is typed and is never asked for a dialog.
  const noShortcutDialog = $derived(portalBackend && hotkeyStatus?.configure_supported === false);
  // A disabled button cannot show the title the reason is in, so the reason is
  // also read out as the description of the button, and the line that carries it
  // is the one that is named. It is named only once it is on screen.
  const dialogReason = $derived(noShortcutDialog && guidanceText ? GUIDANCE_ID : undefined);
  // One line carries whatever the last action left behind, so a reason is read
  // where it was produced instead of in a window the bind flow never opens.
  const statusLine = $derived(statusNote || (triggerUnbound ? UNBOUND_NOTE : ''));
  const statusLineTone = $derived<StatusTone>(statusNote ? statusTone : 'warning');

  // The draw inside the icon is the feedback, so each state is a different icon
  // rather than a different animation on the same one.
  function iconState(row: TriggerRow): IconState {
    if (flashRow === row) return 'flash';
    if (waitingChoice && waitingRow === row) return 'waiting';
    return 'idle';
  }

  // A button that is off because the desktop has no dialog says why, and points
  // at the line below that says what to do instead. The pointer reads it from the
  // button when it is live and from the box around it when it is not, because a
  // disabled button shows no title of its own.
  function bindTitle(row: TriggerRow): string {
    if (savingHotkey || waitingChoice) return WAITING_TITLE;
    if (noShortcutDialog) return NO_DIALOG_TITLE;
    return row === 'capture' ? CAPTURE_TRIGGER_TITLE : REGION_TRIGGER_TITLE;
  }

  async function loadGuidance(): Promise<void> {
    if (guidanceAsked) return;
    guidanceAsked = true;
    try {
      guidanceText = await invoke<string>('configure_guidance_text');
    } catch (e: unknown) {
      statusNote = String(e);
      statusTone = 'error';
    }
  }

  $effect(() => {
    if (noShortcutDialog) void loadGuidance();
  });

  function showFlash(row: TriggerRow): void {
    flashRow = row;
    if (flashTimer !== null) clearTimeout(flashTimer);
    flashTimer = setTimeout(() => {
      flashRow = null;
      flashTimer = null;
    }, FLASH_MS);
  }

  async function loadTrigger(row: TriggerRow): Promise<void> {
    try {
      const value = await invoke<string>(TRIGGER_COMMANDS[row].read);
      if (row === 'capture') newHotkey = value;
      else newSelectHotkey = value;
    } catch (e: unknown) {
      statusNote = String(e);
      statusTone = 'error';
    }
  }

  async function saveTrigger(row: TriggerRow): Promise<void> {
    if (savingHotkey) return;
    savingHotkey = true;
    statusNote = '';
    try {
      // The backend hands back the shortcut it actually holds, which on a portal
      // is not the one typed, so the field follows it instead of the keystrokes.
      const bound = await invoke<string>(TRIGGER_COMMANDS[row].write, {
        hotkey: row === 'capture' ? newHotkey : newSelectHotkey,
      });
      if (row === 'capture') newHotkey = bound;
      else newSelectHotkey = bound;
      statusNote = TRIGGER_COMMANDS[row].saved;
      statusTone = 'info';
      showFlash(row);
    } catch (e: unknown) {
      statusNote = String(e);
      statusTone = 'error';
    } finally {
      savingHotkey = false;
      waitingChoice = false;
      waitingRow = null;
    }
  }

  async function close(): Promise<void> {
    try {
      await getCurrentWindow().close();
    } catch (e: unknown) {
      statusNote = String(e);
      statusTone = 'error';
    }
  }

  async function setDontShow(hide: boolean): Promise<void> {
    try {
      dontShow = await invoke<boolean>('set_hide_bind_notice', { hide });
    } catch (e: unknown) {
      statusNote = String(e);
      statusTone = 'error';
      return;
    }
    // Checking the box only records the preference; the window stays open
    // until Close is clicked, so an accidental tick never dismisses it.
  }

  function onDismissChange(event: Event): void {
    const box = event.currentTarget as HTMLInputElement;
    void setDontShow(box.checked);
  }

  onMount(() => {
    void loadTrigger('capture');
    void loadTrigger('region');
    invoke<boolean>('get_hide_bind_notice')
      .then((value) => {
        dontShow = value;
      })
      .catch((e: unknown) => {
        statusNote = String(e);
        statusTone = 'error';
      });
    invoke<HotkeyStatus>('hotkey_status')
      .then((value) => {
        hotkeyStatus = value;
      })
      .catch((e: unknown) => {
        statusNote = String(e);
        statusTone = 'error';
      });
    void listen<HotkeyStatus>('hotkey-status', (e) => {
      hotkeyStatus = e.payload;
    })
      .then((unlisten) => {
        registered.push(unlisten);
      })
      .catch((e: unknown) => {
        statusNote = String(e);
        statusTone = 'error';
      });
    void listen<string>('hotkey-dialog-opened', (e) => {
      savingHotkey = false;
      waitingChoice = true;
      waitingRow = ROWS_BY_ID[e.payload] ?? null;
    })
      .then((unlisten) => {
        registered.push(unlisten);
      })
      .catch((e: unknown) => {
        statusNote = String(e);
        statusTone = 'error';
      });
    const motion = window.matchMedia(REDUCED_MOTION_QUERY);
    reducedMotion = motion.matches;
    const onMotionChange = (change: MediaQueryListEvent) => {
      reducedMotion = change.matches;
    };
    motion.addEventListener('change', onMotionChange);
    return () => {
      for (const unlisten of registered) unlisten();
      motion.removeEventListener('change', onMotionChange);
      if (flashTimer !== null) clearTimeout(flashTimer);
    };
  });
</script>

<main>
  {#snippet triggerIcon(row: TriggerRow)}
    {#if reducedMotion}
      {#if flashRow === row}
        <Check size={ICON_SIZE} />
      {:else}
        <Keyboard size={ICON_SIZE} />
      {/if}
    {:else}
      {#key iconState(row)}
        {#if iconState(row) === 'flash'}
          <CheckFlowbite size={ICON_SIZE} event="none" />
        {:else if iconState(row) === 'waiting'}
          <HourglassFlowbite size={ICON_SIZE} event="none" />
        {:else}
          <KeyboardFlowbite size={ICON_SIZE} event="none" />
        {/if}
      {/key}
    {/if}
  {/snippet}

  <ol class="steps">
    <li>
      <div class="row">
        {#if portalBackend}
          <span class="rowlabel">
            Capture
            <span class="bound">{captureTrigger ?? NOT_BOUND}</span>
          </span>
        {:else}
          <label class="rowlabel">
            Capture
            <input bind:value={newHotkey} placeholder={CAPTURE_PLACEHOLDER} />
          </label>
        {/if}
        <span class="bindwrap" title={noShortcutDialog ? NO_DIALOG_TITLE : undefined}>
          <button
            class="icon bind"
            aria-label={CAPTURE_TRIGGER_TITLE}
            aria-describedby={dialogReason}
            title={bindTitle('capture')}
            onclick={() => saveTrigger('capture')}
            disabled={savingHotkey || waitingChoice || noShortcutDialog}
          >
            {@render triggerIcon('capture')}
          </button>
        </span>
      </div>
    </li>
    <li>
      <div class="row">
        {#if portalBackend}
          <span class="rowlabel">
            Region
            <span class="bound">{selectTrigger ?? NOT_BOUND}</span>
          </span>
        {:else}
          <label class="rowlabel">
            Region
            <input bind:value={newSelectHotkey} placeholder={REGION_PLACEHOLDER} />
          </label>
        {/if}
        <span class="bindwrap" title={noShortcutDialog ? NO_DIALOG_TITLE : undefined}>
          <button
            class="icon bind"
            aria-label={REGION_TRIGGER_TITLE}
            aria-describedby={dialogReason}
            title={bindTitle('region')}
            onclick={() => saveTrigger('region')}
            disabled={savingHotkey || waitingChoice || noShortcutDialog}
          >
            {@render triggerIcon('region')}
          </button>
        </span>
      </div>
    </li>
  </ol>

  <p class="hintline">
    {waitingChoice ? DIALOG_WAIT_HINT : portalBackend ? stepHint : CAPTURE_STEP_HINT}
  </p>

  {#if noShortcutDialog && guidanceText}
    <p id={GUIDANCE_ID} class="guidance" role="status">{guidanceText}</p>
  {/if}

  {#if statusLine}
    <p class="statusline" data-tone={statusLineTone} role="status">{statusLine}</p>
  {/if}

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
          GOaT.
          {#if noShortcutDialog}
            This desktop has no dialog for the buttons to open, so they are off
            until a trigger is bound there.
          {:else}
            The buttons open that dialog.
          {/if}
          On KDE Plasma: System Settings → Keyboard → Shortcuts. On GNOME: Settings
          → Keyboard → View and Customize Shortcuts.
        </p>
      {/if}
      {#if hotkeyStatus}
        <p class="hotkeyline" data-backend={hotkeyStatus.backend}>
          {hotkeyStatus.detail}
        </p>
        {#if hotkeyStatus.warning}
          <p class="statusline" data-tone="warning" role="status">{hotkeyStatus.warning}</p>
        {/if}
      {/if}
    </div>
  </details>

  <div class="dismiss">
    <label>
      <input type="checkbox" checked={dontShow} onchange={onDismissChange} />
      Don't show this again
    </label>
    <button class="ghost" onclick={close}>Close</button>
  </div>
</main>

<style>
  :global(html),
  :global(body) {
    margin: 0;
    height: 100%;
    background: #1b1d21;
    color: #fff;
    font-family: system-ui, sans-serif;
  }

  main {
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
    min-height: 100vh;
    padding: 0.7rem 0.9rem 0;
  }

  .steps {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin: 0;
    padding-left: 1.3rem;
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

  .hintline {
    margin: 0.7rem 0 0;
    font-size: 0.8rem;
    color: #c9ccd2;
  }

  .guidance {
    align-self: flex-start;
    margin: 0;
    padding: 0.25rem 0.6rem;
    background: rgba(0, 0, 0, 0.45);
    border-left: 3px solid #ffc46b;
    border-radius: 0.4rem;
    font-size: 0.8rem;
    color: #c9ccd2;
  }

  .statusline {
    align-self: flex-end;
    margin: 0;
    max-width: 100%;
    text-align: right;
    font-size: 0.8rem;
    color: #c9ccd2;
  }

  .statusline[data-tone='warning'] {
    color: #ffc46b;
  }

  .statusline[data-tone='error'] {
    color: #ff9d9d;
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
    margin-top: 0.35rem;
    padding-top: 0.4rem;
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
    position: sticky;
    bottom: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    margin-top: auto;
    padding: 0.55rem 0.9rem 0.7rem;
    border-top: 1px solid rgba(255, 255, 255, 0.14);
    background: #14161a;
    font-size: 0.8rem;
  }

  button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid transparent;
    border-radius: 0.4rem;
    font: inherit;
    cursor: pointer;
  }

  button:disabled {
    opacity: 0.45;
    cursor: default;
  }

  /* The box around a Choose button, so a disabled one still has a hoverable
     surface to carry the reason: a disabled button shows no title of its own.
     The push to the far end belongs here rather than on the button, which is no
     longer a direct child of the row. */
  .bindwrap {
    display: inline-flex;
    margin-left: auto;
  }

  .bind {
    min-width: 2.25rem;
    min-height: 2.25rem;
    padding: 0;
    background: #2f6bd8;
    border-color: #2f6bd8;
    color: #fff;
  }

  .bind:hover:not(:disabled) {
    background: #3b7ae8;
  }

  .ghost {
    min-height: 2.25rem;
    padding: 0 0.9rem;
    background: transparent;
    border-color: rgba(255, 255, 255, 0.28);
    color: #c9ccd2;
  }

  .ghost:hover {
    background: rgba(255, 255, 255, 0.08);
    color: #fff;
  }
</style>
