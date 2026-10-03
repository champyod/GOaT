/// The two triggers the backend can be asked to bind.
export type TriggerRow = 'capture' | 'region';

export type StatusTone = 'info' | 'warning' | 'error';

/// The draw inside a trigger icon is the feedback, so each state is a different
/// icon rather than a different animation on the same one.
export type IconState = 'idle' | 'waiting' | 'flash';

/// The account the backend gives of the session. A trigger is never read out of
/// the stored config alone, because a portal session ignores anything typed.
export type HotkeyStatus = {
  backend: 'system' | 'portal';
  detail: string;
  warning: string;
  capture_trigger: string | null;
  select_trigger: string | null;
  /// Whether the desktop can be asked for its own shortcut dialog. A portal
  /// reporting an interface old enough to predate that dialog cannot be, so
  /// there is nothing behind a Choose button there and the guidance replaces it.
  /// A window-system session never asks and is never held to it. Null is a
  /// session that has not reported yet, which keeps the buttons live rather than
  /// switching them off on a desktop that does have a dialog.
  configure_supported: boolean | null;
};

/// What a window reads but does not own: the flags that decide how a row draws
/// itself, and the text each state says about itself. No markup is here, so one
/// object serves a window of two stacked rows and a narrow panel of two rows.
export type KeybindView = {
  status: HotkeyStatus | null;
  portalBackend: boolean;
  captureTrigger: string | null;
  selectTrigger: string | null;
  triggerUnbound: boolean;
  noShortcutDialog: boolean;
  guidanceText: string;
  guidanceId: string | undefined;
  saving: boolean;
  waiting: boolean;
  waitingRow: TriggerRow | null;
  bindDisabled: boolean;
  hintLine: string;
  statusLine: string;
  statusLineTone: StatusTone;
};

export type KeybindOptions = {
  /// Where a failure goes besides the status line. The reason always reaches the
  /// line, so a window that also keeps a record of its own hands it here.
  onError?: (source: string, message: string) => void;
};

export type KeybindConfig = {
  readonly view: KeybindView;
  field: (row: TriggerRow) => string;
  setField: (row: TriggerRow, value: string) => void;
  bindTitle: (row: TriggerRow) => string;
  iconState: (row: TriggerRow) => IconState;
  start: () => Promise<void>;
  stop: () => void;
  loadTrigger: (row: TriggerRow) => Promise<void>;
  saveTrigger: (row: TriggerRow) => Promise<void>;
  requestCaptureTrigger: () => Promise<void>;
};

export const CAPTURE_TRIGGER_TITLE = 'Choose capture trigger';
export const REGION_TRIGGER_TITLE = 'Choose region trigger';
/// A button that is off because the desktop has no dialog to open has to say so
/// where the pointer is, and the guidance under it is what the title points at.
export const NO_DIALOG_TITLE = 'This desktop has no shortcut dialog — see below';
/// The guidance the two buttons point at when they are off, and the element the
/// description of a disabled button names.
export const GUIDANCE_ID = 'no-shortcut-dialog-guidance';
export const WAITING_TITLE = 'Waiting...';
export const DIALOG_WAIT_HINT = 'Desktop dialog open — pick keys there.';
export const CAPTURE_PLACEHOLDER = 'Ctrl+Shift+S';
export const REGION_PLACEHOLDER = 'Ctrl+Shift+E';
export const CAPTURE_STEP_HINT = 'Press your capture shortcut to grab the screen.';
export const UNBOUND_HINT = 'Once bound, press it to grab the screen.';
export const NOT_BOUND = 'Not bound';
export const UNBOUND_NOTE = 'No shortcut bound yet.';

export const FLASH_MS = 900;

/// The command pair for a row, and the note a save that reached the backend
/// leaves behind.
type TriggerCommand = { read: string; write: string; saved: string };

export const TRIGGER_COMMANDS: Record<TriggerRow, TriggerCommand> = {
  capture: { read: 'get_hotkey', write: 'set_hotkey', saved: 'Capture trigger saved' },
  region: {
    read: 'get_select_hotkey',
    write: 'set_select_hotkey',
    saved: 'Region trigger saved'
  }
};

/// The portal names the shortcut it is reconfiguring, so the wait lands on the
/// row that asked for it rather than on both. These mirror the ids the backend
/// holds in `binding::CAPTURE_ID` and `binding::SELECT_ID`.
export const ROWS_BY_ID: Record<string, TriggerRow> = {
  goat_capture: 'capture',
  goat_region_select: 'region'
};