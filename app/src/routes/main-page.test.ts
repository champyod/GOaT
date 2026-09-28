import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import Main from './+page.svelte';
import {
  clipboardWrite,
  drain,
  installTauriMock,
  lastPaintedFrame
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
