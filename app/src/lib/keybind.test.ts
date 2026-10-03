import { describe, expect, it, vi } from 'vitest';

import {
  CAPTURE_STEP_HINT,
  DIALOG_WAIT_HINT,
  GUIDANCE_ID,
  NO_DIALOG_TITLE,
  UNBOUND_HINT,
  UNBOUND_NOTE,
  WAITING_TITLE,
  createKeybindConfig,
  type HotkeyStatus,
  type KeybindConfig
} from './keybind.svelte';
import { drain, installTauriMock, type TauriHarness } from '../test-setup';

const GUIDANCE = 'Bind the trigger in your desktop shortcut settings.';

function status(overrides: Partial<HotkeyStatus> = {}): HotkeyStatus {
  return {
    backend: 'system',
    detail: 'window-system session',
    warning: '',
    capture_trigger: 'Ctrl+Shift+S',
    select_trigger: 'Ctrl+Shift+E',
    configure_supported: null,
    ...overrides
  };
}

const PORTAL_NO_DIALOG = status({ backend: 'portal', configure_supported: false });

/// Every spec but the first drives the machine through its feeds, so they all
/// need the same window-system session started, and torn down on the way out.
async function started(
  overrides: Parameters<typeof installTauriMock>[0] = {}
): Promise<{ keybind: KeybindConfig; harness: TauriHarness; unstop: () => void }> {
  const harness = installTauriMock(overrides);
  const keybind = createKeybindConfig();
  await keybind.start();
  return { keybind, harness, unstop: () => keybind.stop() };
}

/// A command the spec holds open, so the flag it is about is readable while the
/// call is still in flight.
function heldOpen(): { pending: Promise<string>; release: () => void } {
  let release = (): void => {};
  const pending = new Promise<string>((resolve) => {
    release = () => resolve('Ctrl+Shift+S');
  });
  return { pending, release };
}

describe('loading and saving a trigger', () => {
  it('puts the shortcut each row holds into its own field', async () => {
    installTauriMock({ get_hotkey: 'Ctrl+Alt+S', get_select_hotkey: 'Ctrl+Alt+E' });
    const keybind = createKeybindConfig();
    await keybind.start();

    expect(keybind.field('capture')).toBe('Ctrl+Alt+S');
    expect(keybind.field('region')).toBe('Ctrl+Alt+E');
    keybind.stop();
  });

  it('keeps the shortcut the backend echoes back, not the one typed', async () => {
    installTauriMock({ set_hotkey: 'Super+1' });
    const keybind = createKeybindConfig();
    keybind.setField('capture', 'Ctrl+Alt+S');
    await keybind.saveTrigger('capture');

    expect(keybind.field('capture')).toBe('Super+1');
    expect(keybind.iconState('capture')).toBe('flash');
    keybind.stop();
  });

  it('sends a row through the command of its own trigger', async () => {
    const harness = installTauriMock({ set_select_hotkey: 'Super+2' });
    const keybind = createKeybindConfig();
    keybind.setField('region', 'Ctrl+Alt+E');
    await keybind.saveTrigger('region');

    expect(harness.argsOf('set_select_hotkey')).toEqual([{ hotkey: 'Ctrl+Alt+E' }]);
    expect(keybind.field('region')).toBe('Super+2');
    keybind.stop();
  });

  it('records a refusal on the status line and hands it over', async () => {
    installTauriMock({
      set_hotkey: () => {
        throw new Error('that shortcut needs a modifier');
      }
    });
    const onError = vi.fn<(source: string, message: string) => void>();
    const keybind = createKeybindConfig({ onError });
    await keybind.saveTrigger('capture');

    expect(keybind.view.statusLine).toBe('Error: that shortcut needs a modifier');
    expect(keybind.view.statusLineTone).toBe('error');
    expect(onError).toHaveBeenCalledWith('hotkey', 'Error: that shortcut needs a modifier');
    keybind.stop();
  });

  it('prefers what a save left behind over what the session warns about', async () => {
    const { keybind, unstop } = await started({ hotkey_status: status({ warning: 'none' }) });
    await keybind.saveTrigger('capture');

    expect(keybind.view.statusLine).toBe('Capture trigger saved');
    expect(keybind.view.statusLineTone).toBe('info');
    unstop();
  });
});

describe('the feeds the machine follows', () => {
  it('takes the backend status as it changes', async () => {
    const { keybind, harness, unstop } = await started();
    expect(keybind.view.portalBackend).toBe(false);

    await harness.emit('hotkey-status', status({ backend: 'portal' }));
    await drain();

    expect(keybind.view.portalBackend).toBe(true);
    expect(keybind.view.status?.backend).toBe('portal');
    unstop();
  });

  it('waits on the row whose shortcut the desktop dialog opened', async () => {
    const { keybind, harness, unstop } = await started();
    await harness.emit('hotkey-dialog-opened', 'goat_region_select');
    await drain();

    expect(keybind.view.waiting).toBe(true);
    expect(keybind.view.waitingRow).toBe('region');
    expect(keybind.iconState('region')).toBe('waiting');
    expect(keybind.iconState('capture')).toBe('idle');
    expect(keybind.view.hintLine).toBe(DIALOG_WAIT_HINT);
    expect(keybind.view.bindDisabled).toBe(true);
    expect(keybind.bindTitle('region')).toBe(WAITING_TITLE);
    unstop();
  });

  it('reports the dialog wait rather than the call still in flight', async () => {
    const held = heldOpen();
    const { keybind, harness, unstop } = await started({ set_hotkey: () => held.pending });

    const saving = keybind.saveTrigger('capture');
    await drain();
    expect(keybind.view.saving).toBe(true);

    await harness.emit('hotkey-dialog-opened', 'goat_capture');
    await drain();
    expect(keybind.view.saving).toBe(false);
    expect(keybind.view.waitingRow).toBe('capture');

    held.release();
    await saving;
    expect(keybind.view.saving).toBe(false);
    expect(keybind.view.waiting).toBe(false);
    expect(keybind.view.waitingRow).toBeNull();
    unstop();
  });

  it('waits on no row for a shortcut it cannot name', async () => {
    const { keybind, harness, unstop } = await started();
    await harness.emit('hotkey-dialog-opened', 'goat_something_else');
    await drain();

    expect(keybind.view.waiting).toBe(true);
    expect(keybind.view.waitingRow).toBeNull();
    expect(keybind.iconState('capture')).toBe('idle');
    unstop();
  });
});

describe('what each session is read as', () => {
  it('tells a portal session from a window-system one by its rows', async () => {
    const { keybind, harness, unstop } = await started();
    expect(keybind.view.hintLine).toBe(CAPTURE_STEP_HINT);

    await harness.emit('hotkey-status', status({ backend: 'portal', capture_trigger: null }));
    await drain();

    expect(keybind.view.portalBackend).toBe(true);
    expect(keybind.view.hintLine).toBe(UNBOUND_HINT);
    unstop();
  });

  it('reads a window-system session as unbound on its warning alone', async () => {
    const { keybind, harness, unstop } = await started();
    expect(keybind.view.statusLine).toBe('');

    await harness.emit('hotkey-status', status({ warning: 'no trigger is bound' }));
    await drain();

    expect(keybind.view.triggerUnbound).toBe(true);
    expect(keybind.view.statusLine).toBe(UNBOUND_NOTE);
    expect(keybind.view.statusLineTone).toBe('warning');
    unstop();
  });

  it('reads a portal as unbound on the triggers, never on the warning', async () => {
    const { keybind, harness, unstop } = await started();
    await harness.emit(
      'hotkey-status',
      status({ backend: 'portal', warning: 'an old release note' })
    );
    await drain();
    expect(keybind.view.triggerUnbound).toBe(false);
    expect(keybind.view.statusLine).toBe('');

    await harness.emit('hotkey-status', status({ backend: 'portal', select_trigger: null }));
    await drain();
    expect(keybind.view.triggerUnbound).toBe(true);
    expect(keybind.view.statusLine).toBe(UNBOUND_NOTE);
    unstop();
  });

  it('turns the buttons off only on a portal that says it has no dialog', async () => {
    const { keybind, harness, unstop } = await started();
    await harness.emit('hotkey-status', PORTAL_NO_DIALOG);
    await drain();

    expect(keybind.view.noShortcutDialog).toBe(true);
    expect(keybind.view.bindDisabled).toBe(true);
    expect(keybind.bindTitle('capture')).toBe(NO_DIALOG_TITLE);
    expect(keybind.view.guidanceText).toBe(GUIDANCE);
    expect(keybind.view.guidanceId).toBe(GUIDANCE_ID);
    unstop();
  });

  it('leaves a session that cannot answer, or never asks, alone', async () => {
    const { keybind, harness, unstop } = await started();
    await harness.emit('hotkey-status', status({ backend: 'portal', configure_supported: null }));
    await drain();
    expect(keybind.view.bindDisabled).toBe(false);
    expect(keybind.view.guidanceId).toBeUndefined();

    await harness.emit('hotkey-status', status({ configure_supported: false }));
    await drain();
    expect(keybind.view.noShortcutDialog).toBe(false);
    unstop();
  });

  it('asks for the guidance on its own, and only once', async () => {
    const { keybind, harness, unstop } = await started();
    await harness.emit('hotkey-status', PORTAL_NO_DIALOG);
    await drain();
    await harness.emit(
      'hotkey-status',
      status({ backend: 'portal', configure_supported: false, warning: 'still none' })
    );
    await drain();

    const asked = harness.calls.filter((call) => call.cmd === 'configure_guidance_text');
    expect(asked).toHaveLength(1);
    unstop();
  });
});

describe('the capture trigger request', () => {
  it('takes a second request once the first has been handed over', async () => {
    const { keybind, harness, unstop } = await started();
    await keybind.requestCaptureTrigger();
    await keybind.requestCaptureTrigger();

    expect(harness.argsOf('set_hotkey')).toHaveLength(2);
    unstop();
  });

  it('drops a request made while the round is already going', async () => {
    const held = heldOpen();
    const { keybind, harness, unstop } = await started({ set_hotkey: () => held.pending });

    const first = keybind.requestCaptureTrigger();
    await drain();
    await keybind.requestCaptureTrigger();

    expect(harness.argsOf('set_hotkey')).toHaveLength(1);
    held.release();
    await first;
    unstop();
  });

  it('drops a request while the desktop dialog holds the round', async () => {
    const { keybind, harness, unstop } = await started();
    await harness.emit('hotkey-dialog-opened', 'goat_capture');
    await drain();
    await keybind.requestCaptureTrigger();

    expect(harness.argsOf('set_hotkey')).toHaveLength(0);
    unstop();
  });

  it('drops a request on a desktop that has no dialog to open', async () => {
    const { keybind, harness, unstop } = await started();
    await harness.emit('hotkey-status', PORTAL_NO_DIALOG);
    await drain();
    await keybind.requestCaptureTrigger();

    expect(harness.argsOf('set_hotkey')).toHaveLength(0);
    unstop();
  });
});