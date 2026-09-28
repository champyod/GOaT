import { fireEvent, render, screen } from '@testing-library/svelte';
import type { LogicalPosition, Position } from '@tauri-apps/api/dpi';
import { describe, expect, it } from 'vitest';
import Main from './+page.svelte';
import {
  clipboardWrite,
  drain,
  installTauriMock,
  lastPaintedFrame,
  type TauriHarness
} from '../test-setup';

const IMAGE = {
  width: 200,
  height: 200,
  rgba: new Array(200 * 200 * 4).fill(255)
};
const IMAGE_BYTES = 200 * 200 * 4;
/// The canvas is laid out at a quarter of the screenshot, so a region in CSS
/// pixels is read back in image pixels and every number below is that factor.
const SHOT_BOX = { left: 0, top: 0, width: 100, height: 100 };
const SCALE = IMAGE.width / SHOT_BOX.width;

type ResultPayload = {
  image: typeof IMAGE;
  ocr_text: string;
  translated_text: string;
  ocr_engine: string;
  error: string;
};

type Deferred<T> = { promise: Promise<T>; resolve: (value: T) => void };

function result(overrides: Partial<ResultPayload> = {}): ResultPayload {
  return {
    image: IMAGE,
    ocr_text: 'hola',
    translated_text: 'hello',
    ocr_engine: 'tesseract',
    error: '',
    ...overrides
  };
}

function deferred<T>(): Deferred<T> {
  let settle: (value: T) => void = () => undefined;
  const promise = new Promise<T>((resolve) => {
    settle = resolve;
  });
  return { promise, resolve: settle };
}

/// The one place the run is announced, and the only account of the phase there
/// is: the covers come and go with it, so the sentence is what says what a
/// phase means.
function announcedPhase(): string {
  return document.querySelector('p[aria-live="polite"]')?.textContent ?? '';
}

function captureButton(): HTMLButtonElement {
  return screen.getByLabelText<HTMLButtonElement>('Capture');
}

function mustFind<T extends Element>(root: ParentNode, selector: string): T {
  const found = root.querySelector<T>(selector);
  if (found === null) throw new Error(`the page rendered no ${selector}`);
  return found;
}

type Box = { left: number; top: number; width: number; height: number };

/// Where the window was last asked to sit, in the logical points a position is
/// asked for in. One move is what the bar allows itself, so this is read as a
/// list: a second entry is a bar the user has since dragged being taken back.
/// The harness records the call before it is serialized, so the point is read
/// off the position the API wraps rather than off the wire shape.
function placedAt(harness: TauriHarness): { x: number; y: number }[] {
  return harness
    .argsOf('plugin:window|set_position')
    .map((args) => {
      const call = args as { value: Position };
      const point = call.value.position as LogicalPosition;
      return { x: point.x, y: point.y };
    });
}

function stubBox(element: Element, box: Box): void {
  element.getBoundingClientRect = () => ({
    ...box,
    x: box.left,
    y: box.top,
    right: box.left + box.width,
    bottom: box.top + box.height,
    toJSON: () => ({})
  });
}

async function armRegionSelection(): Promise<Element> {
  await fireEvent.click(captureButton());
  const shot = await screen.findByRole('application', {
    name: 'Drag on the screenshot to select a region'
  });
  stubBox(shot, SHOT_BOX);
  stubBox(mustFind<HTMLCanvasElement>(shot, 'canvas'), SHOT_BOX);
  await fireEvent.click(screen.getByLabelText<HTMLButtonElement>('Select region'));
  return shot;
}

async function drag(
  shot: Element,
  from: { x: number; y: number },
  to: { x: number; y: number }
): Promise<void> {
  await fireEvent.mouseDown(shot, { clientX: from.x, clientY: from.y });
  await fireEvent.mouseMove(shot, { clientX: to.x, clientY: to.y });
  await fireEvent.mouseUp(shot);
  await drain();
}

describe('the capture window', () => {
  it('announces nothing until there is something to say', async () => {
    installTauriMock();
    render(Main);
    await drain();
    expect(announcedPhase()).toBe('');
  });

  it('walks idle to done through a capture the button started', async () => {
    const run = deferred<ResultPayload>();
    installTauriMock({ capture_primary: () => run.promise });
    render(Main);
    await drain();

    await fireEvent.click(captureButton());
    await screen.findByText('Capturing the screen.');
    expect(announcedPhase()).toBe('Capturing the screen.');
    expect(captureButton().disabled).toBe(true);

    run.resolve(result());
    await screen.findByText('Capture complete.');
    expect(screen.getByDisplayValue('hola')).toBeDefined();
    expect(screen.getByDisplayValue('hello')).toBeDefined();
    expect(screen.queryByText('Reading…')).toBeNull();
    expect(lastPaintedFrame()).toEqual({
      width: IMAGE.width,
      height: IMAGE.height,
      bytes: IMAGE_BYTES
    });
  });

  it('drops a second press while a run is under way', async () => {
    const run = deferred<ResultPayload>();
    const harness = installTauriMock({ capture_primary: () => run.promise });
    render(Main);
    await drain();

    await fireEvent.click(captureButton());
    await screen.findByText('Capturing the screen.');
    expect(captureButton().disabled).toBe(true);

    await fireEvent.click(captureButton());
    await drain();
    expect(harness.argsOf('capture_primary')).toHaveLength(1);
  });

  it('follows the phases the backend publishes', async () => {
    const harness = installTauriMock();
    render(Main);
    await drain();

    await harness.emit('capture-progress', { phase: 'reading', image: IMAGE });
    await screen.findByText('Reading text from the screenshot.');
    expect(screen.getByText('Reading…')).toBeDefined();

    await harness.emit('capture-progress', { phase: 'translating', ocr_text: 'hola' });
    await screen.findByText('Translating the text that was read.');
    expect(screen.getByText('Translating…')).toBeDefined();
    expect(screen.queryByText('Reading…')).toBeNull();

    await harness.emit('capture-progress', { phase: 'done', payload: result() });
    await screen.findByText('Capture complete.');
    expect(screen.getByDisplayValue('hello')).toBeDefined();
  });

  it('ends the run on the failure the backend publishes', async () => {
    const harness = installTauriMock();
    render(Main);
    await drain();

    await harness.emit('capture-progress', { phase: 'error', error: 'the sidecar died' });
    await screen.findByText('The capture failed.');
    expect(harness.argsOf('report_frontend_error')).toEqual([
      { source: 'capture', message: 'the sidecar died' }
    ]);
  });

  it('keeps the copy buttons off while there is no text to copy', async () => {
    installTauriMock({ capture_primary: () => result({ ocr_text: '', translated_text: '' }) });
    render(Main);
    await drain();

    await fireEvent.click(captureButton());
    await screen.findByText('Capture complete.');
    expect(screen.getByLabelText<HTMLButtonElement>("Copy OCR'ed text").disabled).toBe(true);
    expect(screen.getByLabelText<HTMLButtonElement>('Copy translated text').disabled).toBe(true);
  });

  it('copies the field the button belongs to', async () => {
    installTauriMock({ capture_primary: () => result() });
    render(Main);
    await drain();

    await fireEvent.click(captureButton());
    await screen.findByText('Capture complete.');
    const ocr = screen.getByLabelText<HTMLButtonElement>("Copy OCR'ed text");
    await fireEvent.click(ocr);
    expect(clipboardWrite).toHaveBeenCalledWith('hola');

    await fireEvent.click(screen.getByLabelText<HTMLButtonElement>('Copy translated text'));
    expect(clipboardWrite).toHaveBeenCalledWith('hello');
  });
});

/// The bar is placed once, against the monitor captures are taken from and the
/// distance from the top the user chose. Both are stored values, so a move made
/// before either has answered is a move that is never corrected.
describe('placing the bar', () => {
  const SCREENS = [
    { index: 0, name: 'DP-1', is_primary: true, width: 1920, height: 1080, x: 0, y: 0 },
    {
      index: 1,
      name: 'HDMI-1',
      is_primary: false,
      width: 1920,
      height: 1080,
      x: 1920,
      y: 0
    }
  ];

  /// The window is 300 points wide on a screen 1920 across, so the side monitor
  /// at x=1920 centres the bar at 1920 + (1920 - 300) / 2 = 2730.
  it('centres the bar on the monitor captures are taken from', async () => {
    const harness = installTauriMock({
      list_monitors: SCREENS,
      get_monitor: 1,
      get_bar_top_offset: 64
    });
    render(Main);
    await drain();

    expect(placedAt(harness)).toEqual([{ x: 2730, y: 64 }]);
  });

  it('falls back to the primary screen when the stored monitor is not listed', async () => {
    const harness = installTauriMock({
      list_monitors: SCREENS,
      get_monitor: 7,
      get_bar_top_offset: 28
    });
    render(Main);
    await drain();

    expect(placedAt(harness)).toEqual([{ x: 810, y: 28 }]);
  });

  it('waits for the stored distance rather than placing the bar at the default', async () => {
    const offset = deferred<number>();
    const harness = installTauriMock({
      list_monitors: SCREENS,
      get_monitor: 0,
      get_bar_top_offset: () => offset.promise
    });
    render(Main);
    await drain();
    expect(placedAt(harness)).toEqual([]);

    offset.resolve(120);
    await drain();
    expect(placedAt(harness)).toEqual([{ x: 810, y: 120 }]);
  });
});

describe('reading a region of the screenshot', () => {
  it('reads nothing from a drag under four pixels', async () => {
    const harness = installTauriMock({ capture_primary: () => result() });
    render(Main);
    await drain();
    const shot = await armRegionSelection();

    await drag(shot, { x: 10, y: 10 }, { x: 12, y: 12 });
    expect(harness.argsOf('ocr_selection')).toHaveLength(0);
  });

  it('reads a four pixel drag at the scale of the screenshot', async () => {
    const harness = installTauriMock({ capture_primary: () => result() });
    render(Main);
    await drain();
    const shot = await armRegionSelection();

    await drag(shot, { x: 20, y: 20 }, { x: 24, y: 24 });
    expect(harness.argsOf('ocr_selection')).toEqual([
      { x: 20 * SCALE, y: 20 * SCALE, width: 4 * SCALE, height: 4 * SCALE }
    ]);
  });

  it('clamps a drag that runs past the edge of the screenshot', async () => {
    const harness = installTauriMock({ capture_primary: () => result() });
    render(Main);
    await drain();
    const shot = await armRegionSelection();

    await drag(shot, { x: 10, y: 10 }, { x: 500, y: 500 });
    expect(harness.argsOf('ocr_selection')).toEqual([
      {
        x: 10 * SCALE,
        y: 10 * SCALE,
        width: IMAGE.width - 10 * SCALE,
        height: IMAGE.height - 10 * SCALE
      }
    ]);
  });
});
