import { invoke } from '@tauri-apps/api/core';
import { listen, type UnlistenFn } from '@tauri-apps/api/event';

import {
  CAPTURE_PLACEHOLDER,
  CAPTURE_STEP_HINT,
  CAPTURE_TRIGGER_TITLE,
  DIALOG_WAIT_HINT,
  FLASH_MS,
  GUIDANCE_ID,
  NO_DIALOG_TITLE,
  REGION_PLACEHOLDER,
  REGION_TRIGGER_TITLE,
  ROWS_BY_ID,
  TRIGGER_COMMANDS,
  UNBOUND_HINT,
  UNBOUND_NOTE,
  WAITING_TITLE,
  type HotkeyStatus,
  type IconState,
  type KeybindConfig,
  type KeybindOptions,
  type KeybindView,
  type StatusTone,
  type TriggerRow
} from './keybind-types';

export * from './keybind-types';

/// The whole of the trigger machine, with no markup in it. A window binds its
/// fields to `field`, draws its rows from `view`, and hands a press to
/// `saveTrigger` or `requestCaptureTrigger`.
export function createKeybindConfig(options: KeybindOptions = {}): KeybindConfig {
  let captureField = $state(CAPTURE_PLACEHOLDER);
  let selectField = $state(REGION_PLACEHOLDER);
  let status = $state<HotkeyStatus | null>(null);
  let statusNote = $state('');
  let statusTone = $state<StatusTone>('info');
  let saving = $state(false);
  // The desktop's own dialog owns the round from the moment it opens, so the
  // button reports a wait the user can see somewhere else instead of a call that
  // is still in flight here.
  let waitingChoice = $state(false);
  let waitingRow = $state<TriggerRow | null>(null);
  let flashRow = $state<TriggerRow | null>(null);
  // The line for a desktop with no dialog. The bind that would have produced it
  // is the very button that is disabled, so it is asked for on its own, and only
  // once: the verdict comes from the backend and does not change mid-session.
  let guidanceText = $state('');
  let guidanceAsked = false;
  let flashTimer: ReturnType<typeof setTimeout> | null = null;
  const registered: UnlistenFn[] = [];

  function fail(source: string, reason: unknown): void {
    const message = String(reason);
    statusNote = message;
    statusTone = 'error';
    options.onError?.(source, message);
  }

  function field(row: TriggerRow): string {
    return row === 'capture' ? captureField : selectField;
  }

  function setField(row: TriggerRow, value: string): void {
    if (row === 'capture') captureField = value;
    else selectField = value;
  }

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
    if (saving || waitingChoice) return WAITING_TITLE;
    if (noShortcutDialog) return NO_DIALOG_TITLE;
    return row === 'capture' ? CAPTURE_TRIGGER_TITLE : REGION_TRIGGER_TITLE;
  }

  function showFlash(row: TriggerRow): void {
    flashRow = row;
    if (flashTimer !== null) clearTimeout(flashTimer);
    flashTimer = setTimeout(() => {
      flashRow = null;
      flashTimer = null;
    }, FLASH_MS);
  }

  async function loadGuidance(): Promise<void> {
    if (guidanceAsked) return;
    guidanceAsked = true;
    try {
      guidanceText = await invoke<string>('configure_guidance_text');
    } catch (e: unknown) {
      fail('hotkey', e);
    }
  }

  function applyStatus(line: HotkeyStatus): void {
    status = line;
    if (line.backend === 'portal' && line.configure_supported === false) {
      void loadGuidance();
    }
  }

  async function loadTrigger(row: TriggerRow): Promise<void> {
    try {
      setField(row, await invoke<string>(TRIGGER_COMMANDS[row].read));
    } catch (e: unknown) {
      fail('hotkey', e);
    }
  }

  /// Both rows bind through the same machine: a window-system session registers the
  /// shortcut it is handed, a portal session ignores it and opens the desktop's own
  /// dialog instead. Either way the field follows what the backend says it holds.
  async function saveTrigger(row: TriggerRow): Promise<void> {
    if (saving) return;
    saving = true;
    statusNote = '';
    try {
      const bound = await invoke<string>(TRIGGER_COMMANDS[row].write, { hotkey: field(row) });
      setField(row, bound);
      statusNote = TRIGGER_COMMANDS[row].saved;
      statusTone = 'info';
      showFlash(row);
    } catch (e: unknown) {
      fail('hotkey', e);
    } finally {
      saving = false;
      waitingChoice = false;
      waitingRow = null;
    }
  }

  /// The entry a press on the capture trigger button goes through on every backend: a
  /// click and a hotkey are one request, and once it has been handed to the desktop
  /// or to the window system the round is released, so the next one is taken.
  async function requestCaptureTrigger(): Promise<void> {
    if (bindDisabled) return;
    await saveTrigger('capture');
  }

  function subscribe<T>(event: string, handler: (payload: T) => void): Promise<void> {
    return listen<T>(event, (e) => handler(e.payload))
      .then((unlisten) => {
        registered.push(unlisten);
      })
      .catch((e: unknown) => {
        fail('event', `the ${event} feed could not be opened: ${String(e)}`);
      });
  }

  async function start(): Promise<void> {
    await Promise.all([
      loadTrigger('capture'),
      loadTrigger('region'),
      invoke<HotkeyStatus>('hotkey_status')
        .then(applyStatus)
        .catch((e: unknown) => fail('hotkey', e)),
      subscribe<HotkeyStatus>('hotkey-status', applyStatus),
      subscribe<string>('hotkey-dialog-opened', (id) => {
        saving = false;
        waitingChoice = true;
        waitingRow = ROWS_BY_ID[id] ?? null;
      })
    ]);
  }

  function stop(): void {
    for (const unlisten of registered) unlisten();
    registered.length = 0;
    if (flashTimer !== null) clearTimeout(flashTimer);
    flashTimer = null;
  }

  // A portal session fires only the trigger its own dialog produced, so the saved
  // value is not what is bound and the row shows it as read-only text.
  const portalBackend = $derived(status?.backend === 'portal');
  const captureTrigger = $derived(status?.capture_trigger ?? null);
  const selectTrigger = $derived(status?.select_trigger ?? null);
  // "Unbound" means a trigger is genuinely missing — never just that a warning
  // exists, so a stale-release note after a good remap is not "No shortcut bound yet."
  const triggerUnbound = $derived(
    portalBackend
      ? captureTrigger === null || selectTrigger === null
      : Boolean(status?.warning)
  );
  // Only a "no" turns the buttons off: a session that has not reported yet is
  // left alone, because a desktop that does have a dialog would then be showing a
  // button that cannot work. A window-system session binds what is typed and is
  // never asked for a dialog.
  const noShortcutDialog = $derived(portalBackend && status?.configure_supported === false);
  // A disabled button cannot show the title the reason is in, so the reason is also
  // read out as its description — but only once the line is on screen to name.
  const guidanceId = $derived(noShortcutDialog && guidanceText ? GUIDANCE_ID : undefined);
  // One line carries whatever the last action left behind, so a reason is read
  // where it was produced instead of in a window the bind flow never opens.
  const statusLine = $derived(statusNote || (triggerUnbound ? UNBOUND_NOTE : ''));
  const statusLineTone = $derived<StatusTone>(statusNote ? statusTone : 'warning');
  const hintLine = $derived(
    waitingChoice
      ? DIALOG_WAIT_HINT
      : portalBackend && captureTrigger === null
        ? UNBOUND_HINT
        : CAPTURE_STEP_HINT
  );
  const bindDisabled = $derived(saving || waitingChoice || noShortcutDialog);

  const view = $derived<KeybindView>({
    status,
    portalBackend,
    captureTrigger,
    selectTrigger,
    triggerUnbound,
    noShortcutDialog,
    guidanceText,
    guidanceId,
    saving,
    waiting: waitingChoice,
    waitingRow,
    bindDisabled,
    hintLine,
    statusLine,
    statusLineTone
  });

  return {
    get view() {
      return view;
    },
    field,
    setField,
    bindTitle,
    iconState,
    start,
    stop,
    loadTrigger,
    saveTrigger,
    requestCaptureTrigger
  };
}