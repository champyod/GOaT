import { PhysicalPosition, PhysicalSize } from '@tauri-apps/api/dpi';
import { emit } from '@tauri-apps/api/event';
import { clearMocks, mockIPC, mockWindows } from '@tauri-apps/api/mocks';
import { beforeEach, vi } from 'vitest';

/// A command's answer: a value the mock hands straight back, or a function of
/// the arguments the page passed — a run held open by a test, for one.
export type CommandTable = Record<string, unknown>;

export type InvokeCall = { cmd: string; args: unknown };

export type TauriHarness = {
  calls: InvokeCall[];
  argsOf: (cmd: string) => unknown[];
  emit: (event: string, payload: unknown) => Promise<void>;
};

export type PaintedFrame = { width: number; height: number; bytes: number };

/// Every answer the pages ask for on mount, so a spec only has to name the
/// command it is actually about. A command that is not in this table is a
/// command the harness does not model, and it is refused rather than answered
/// with `undefined` — a page that mistook a refusal for an answer would pass a
/// test it should have failed.
const DEFAULT_COMMANDS: CommandTable = {
  models_status: { ready: true },
  init_models: null,
  list_monitors: [
    {
      index: 0,
      name: 'Test monitor',
      is_primary: true,
      width: 1920,
      height: 1080,
      x: 0,
      y: 0
    }
  ],
  get_monitor: 0,
  set_monitor: 0,
  get_appearance: { accent: null, blur_px: 0, tint_opacity: 72, theme: 'system' },
  set_appearance: { accent: null, blur_px: 0, tint_opacity: 72, theme: 'system' },
  os_platform: 'linux',
  is_autostart: { enabled: false, path: '', is_dev: false },
  set_autostart: {
    enabled: true,
    path: '/home/test/.config/goat.desktop',
    is_dev: false
  },
  hotkey_status: {
    backend: 'system',
    detail: 'window-system session',
    warning: '',
    capture_trigger: 'Ctrl+Shift+S',
    select_trigger: 'Ctrl+Shift+E'
  },
  get_hotkey: 'Ctrl+Shift+S',
  get_select_hotkey: 'Ctrl+Shift+E',
  set_hotkey: 'Ctrl+Shift+S',
  set_select_hotkey: 'Ctrl+Shift+E',
  get_hide_bind_notice: false,
  set_hide_bind_notice: true,
  configure_guidance_text: 'Bind the trigger in your desktop shortcut settings.',
  list_errors: [],
  clear_errors: null,
  report_frontend_error: null,
  set_window_size: null,
  hide_window: null,
  list_webview_windows: ['menu'],
  'plugin:window|outer_size': new PhysicalSize(300, 48),
  'plugin:window|outer_position': new PhysicalPosition(0, 0),
  'plugin:window|scale_factor': 1,
  'plugin:window|is_fullscreen': false,
  'plugin:window|set_size': null,
  'plugin:window|set_decorations': null,
  'plugin:window|set_position': null,
  'plugin:window|set_fullscreen': null,
  'plugin:window|set_effects': null,
  'plugin:window|hide': null,
  'plugin:window|close': null
};

export function installTauriMock(overrides: CommandTable = {}): TauriHarness {
  const table: CommandTable = { ...DEFAULT_COMMANDS, ...overrides };
  const calls: InvokeCall[] = [];
  mockWindows('main', 'menu');
  mockIPC(
    (cmd, args) => {
      calls.push({ cmd, args });
      const reply = table[cmd];
      if (typeof reply === 'function') return (reply as (a: unknown) => unknown)(args);
      if (reply === undefined) throw new Error(`no mock for IPC command: ${cmd}`);
      return reply;
    },
    { shouldMockEvents: true }
  );
  return {
    calls,
    argsOf: (cmd) =>
      calls.filter((call) => call.cmd === cmd).map((call) => call.args),
    emit: (event, payload) => emit(event, payload)
  };
}

/// A macrotask, so every promise the page chained on its way here has settled.
/// Asserting that something did not happen needs an answer for "has it happened
/// yet", and this is the one that does not care how many links are in the chain.
export function drain(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

/// jsdom answers none of these, and a normal run of every page reaches for all
/// of them. A stub stands in for the browser here, so it answers the shape the
/// real one would rather than whatever a test finds convenient.
Object.defineProperty(globalThis, 'ImageData', {
  value: class {
    readonly data: Uint8ClampedArray;
    readonly width: number;
    readonly height: number;
    readonly colorSpace = 'srgb';
    constructor(data: Uint8ClampedArray, width: number, height: number) {
      this.data = data;
      this.width = width;
      this.height = height;
    }
  },
  configurable: true,
  writable: true
});

Object.defineProperty(globalThis, 'ResizeObserver', {
  value: class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  },
  configurable: true,
  writable: true
});

window.matchMedia = (query: string): MediaQueryList => ({
  matches: false,
  media: query,
  onchange: null,
  addEventListener: () => {},
  removeEventListener: () => {},
  addListener: () => {},
  removeListener: () => {},
  dispatchEvent: () => false
});

export const clipboardWrite =
  vi.fn<(text: string) => Promise<void>>(async () => {});

Object.defineProperty(navigator, 'clipboard', {
  value: { writeText: clipboardWrite },
  configurable: true
});

/// The drawing context the screenshot is painted on. jsdom has no pixels, so this
/// records what would have been drawn rather than drawing it: the width and
/// height prove the frame the backend sent reached the canvas, which is the part
/// a test can be held to.
let painted: PaintedFrame | null = null;

export function lastPaintedFrame(): PaintedFrame | null {
  return painted;
}

const CANVAS_2D = {
  putImageData(frame: {
    width: number;
    height: number;
    data: Uint8ClampedArray;
  }): void {
    painted = { width: frame.width, height: frame.height, bytes: frame.data.length };
  }
} as unknown as CanvasRenderingContext2D;

HTMLCanvasElement.prototype.getContext = function getContextStub(
  contextId: string
): unknown {
  return contextId === '2d' ? CANVAS_2D : null;
} as typeof HTMLCanvasElement.prototype.getContext;

/// Reset before each test rather than after one: a component still mounted at
/// the end of a test unsubscribes its event handlers on unmount, and those
/// handlers live in the registry a reset empties — so a reset that ran first
/// would turn the teardown into the failure it exists to prevent.
beforeEach(() => {
  painted = null;
  clearMocks();
});
