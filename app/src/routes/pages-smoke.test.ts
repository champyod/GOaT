import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import Errors from './errors/+page.svelte';
import Menu from './menu/+page.svelte';
import Setup from './setup/+page.svelte';
import { drain, installTauriMock } from '../test-setup';

const STORED_ERRORS = [
  { time: '2026-01-01T09:00:00Z', source: 'monitor', message: 'the older line' },
  { time: '2026-01-02T09:00:00Z', source: 'capture', message: 'the newer line' }
];

const MONITORS = [
  { index: 0, name: 'DP-1', is_primary: true, width: 2560, height: 1440, x: 0, y: 0 },
  { index: 1, name: 'HDMI-1', is_primary: false, width: 1920, height: 1080, x: 2560, y: 0 }
];

describe('the diagnostics window', () => {
  it('lists what the backend kept, newest first', async () => {
    installTauriMock({ list_errors: STORED_ERRORS });
    render(Errors);
    await drain();

    expect(screen.getByRole('heading', { name: 'Errors and warnings' })).toBeDefined();
    const rows = screen.getAllByRole('listitem').map((row) => row.textContent);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toContain('the newer line');
    expect(rows[1]).toContain('the older line');
  });

  it('empties the list when the log is cleared', async () => {
    const harness = installTauriMock({ list_errors: STORED_ERRORS });
    render(Errors);
    await drain();

    await fireEvent.click(screen.getByRole<HTMLButtonElement>('button', { name: 'Clear' }));
    expect(
      await screen.findByText('No errors — everything is running clean.')
    ).toBeDefined();
    expect(harness.argsOf('clear_errors')).toHaveLength(1);
  });
});

describe('the shortcut setup window', () => {
  it('shows the triggers the backend holds', async () => {
    installTauriMock();
    render(Setup);
    await drain();

    expect(screen.getByPlaceholderText<HTMLInputElement>('Ctrl+Shift+S').value).toBe(
      'Ctrl+Shift+S'
    );
    expect(screen.getByPlaceholderText<HTMLInputElement>('Ctrl+Shift+E').value).toBe(
      'Ctrl+Shift+E'
    );
    expect(screen.getByText('Press your capture shortcut to grab the screen.')).toBeDefined();
  });

  it('says so when the backend holds no trigger at all', async () => {
    installTauriMock({
      get_hotkey: '',
      get_select_hotkey: '',
      hotkey_status: {
        backend: 'system',
        detail: 'window-system session',
        warning: 'no trigger is bound',
        capture_trigger: null,
        select_trigger: null
      }
    });
    render(Setup);
    await drain();

    expect(await screen.findByText('No shortcut bound yet.')).toBeDefined();
  });
});

describe('the menu window', () => {
  it('names the monitors the backend reports', async () => {
    installTauriMock({ list_monitors: MONITORS, get_monitor: 0 });
    render(Menu);
    await drain();

    const monitor = screen.getByRole('combobox', { name: 'Monitor' });
    expect(monitor.getAttribute('title')).toBe('DP-1 (primary) 2560x1440');
  });

  it('draws the stored appearance on the window', async () => {
    installTauriMock({
      get_appearance: { accent: '#3f7fd4', blur_px: 0, tint_opacity: 40, theme: 'dark' }
    });
    render(Menu);
    await drain();

    expect(screen.getByLabelText<HTMLInputElement>('Tint').value).toBe('40');
    const root = document.documentElement;
    expect(root.style.getPropertyValue('--goat-accent')).toBe('#3f7fd4');
    expect(root.style.getPropertyValue('color-scheme')).toBe('dark');
  });

  it('shows the triggers the backend holds', async () => {
    installTauriMock();
    render(Menu);
    await drain();

    expect(screen.getByLabelText<HTMLInputElement>('Capture trigger').value).toBe('Ctrl+Shift+S');
    expect(screen.getByLabelText<HTMLInputElement>('Region trigger').value).toBe('Ctrl+Shift+E');
  });

  it('offers the distance the backend holds for the bar', async () => {
    installTauriMock({ get_bar_top_offset: 96 });
    render(Menu);
    await drain();

    expect(screen.getByLabelText<HTMLInputElement>('Bar top').value).toBe('96');
  });

  it('hands the finished distance to the file and shows what it kept', async () => {
    const harness = installTauriMock({ get_bar_top_offset: 28, set_bar_top_offset: 0 });
    render(Menu);
    await drain();

    const slider = screen.getByLabelText<HTMLInputElement>('Bar top');
    await fireEvent.input(slider, { target: { value: '0' } });
    await fireEvent.change(slider);
    expect(harness.argsOf('set_bar_top_offset')).toEqual([{ offset: 0 }]);
    expect(slider.value).toBe('0');
  });
});
