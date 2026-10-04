<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { getCurrentWindow } from '@tauri-apps/api/window';
  import Keyboard from '@lucide/svelte/icons/keyboard';
  import Check from '@lucide/svelte/icons/check';
  import { CheckFlowbite, HourglassFlowbite, KeyboardFlowbite } from 'svelte-animated-icons';

  import {
    CAPTURE_PLACEHOLDER,
    CAPTURE_TRIGGER_TITLE,
    GUIDANCE_ID,
    NO_DIALOG_TITLE,
    NOT_BOUND,
    REGION_PLACEHOLDER,
    REGION_TRIGGER_TITLE,
    createKeybindConfig,
    type StatusTone,
    type TriggerRow
  } from '$lib/keybind.svelte';

  const ICON_SIZE = 16;
  const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)';

  const keybind = createKeybindConfig();
  const view = $derived(keybind.view);

  // What this window says about its own controls, held apart from the trigger
  // machine so a reason is still read out when only this window has one.
  let localNote = $state('');
  let localNoteTone = $state<StatusTone>('info');
  let reducedMotion = $state(false);
  let dontShow = $state(false);

  // One line carries whatever the last action left behind, so a reason is read
  // where it was produced instead of in a window the bind flow never opens.
  const statusLine = $derived(localNote || view.statusLine);
  const statusLineTone = $derived<StatusTone>(
    localNote ? localNoteTone : view.statusLineTone
  );

  function fieldValue(row: TriggerRow): string {
    return keybind.field(row);
  }

  function setFieldValue(row: TriggerRow, value: string): void {
    keybind.setField(row, value);
  }

  async function close(): Promise<void> {
    try {
      await getCurrentWindow().close();
    } catch (e: unknown) {
      localNote = String(e);
      localNoteTone = 'error';
    }
  }

  async function setDontShow(hide: boolean): Promise<void> {
    try {
      dontShow = await invoke<boolean>('set_hide_bind_notice', { hide });
    } catch (e: unknown) {
      localNote = String(e);
      localNoteTone = 'error';
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
    void keybind.start();
    invoke<boolean>('get_hide_bind_notice')
      .then((value) => {
        dontShow = value;
      })
      .catch((e: unknown) => {
        localNote = String(e);
        localNoteTone = 'error';
      });
    const motion = window.matchMedia(REDUCED_MOTION_QUERY);
    reducedMotion = motion.matches;
    const onMotionChange = (change: MediaQueryListEvent) => {
      reducedMotion = change.matches;
    };
    motion.addEventListener('change', onMotionChange);
    return () => {
      keybind.stop();
      motion.removeEventListener('change', onMotionChange);
    };
  });
</script>

<main>
  {#snippet triggerIcon(row: TriggerRow)}
    {@const state = keybind.iconState(row)}
    {#if reducedMotion}
      {#if state === 'flash'}
        <Check size={ICON_SIZE} />
      {:else}
        <Keyboard size={ICON_SIZE} />
      {/if}
    {:else}
      {#key state}
        {#if state === 'flash'}
          <CheckFlowbite size={ICON_SIZE} event="none" />
        {:else if state === 'waiting'}
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
        {#if view.portalBackend}
          <span class="rowlabel">
            Capture
            <span class="bound">{view.captureTrigger ?? NOT_BOUND}</span>
          </span>
        {:else}
          <label class="rowlabel">
            Capture
            <input
              bind:value={() => fieldValue('capture'),
                (value) => setFieldValue('capture', value)}
              placeholder={CAPTURE_PLACEHOLDER}
            />
          </label>
        {/if}
        <span class="bindwrap" title={view.noShortcutDialog ? NO_DIALOG_TITLE : undefined}>
          <button
            class="icon bind"
            aria-label={CAPTURE_TRIGGER_TITLE}
            aria-describedby={view.guidanceId}
            title={keybind.bindTitle('capture')}
            onclick={() => keybind.saveTrigger('capture')}
            disabled={view.bindDisabled}
          >
            {@render triggerIcon('capture')}
          </button>
        </span>
      </div>
    </li>
    <li>
      <div class="row">
        {#if view.portalBackend}
          <span class="rowlabel">
            Region
            <span class="bound">{view.selectTrigger ?? NOT_BOUND}</span>
          </span>
        {:else}
          <label class="rowlabel">
            Region
            <input
              bind:value={() => fieldValue('region'),
                (value) => setFieldValue('region', value)}
              placeholder={REGION_PLACEHOLDER}
            />
          </label>
        {/if}
        <span class="bindwrap" title={view.noShortcutDialog ? NO_DIALOG_TITLE : undefined}>
          <button
            class="icon bind"
            aria-label={REGION_TRIGGER_TITLE}
            aria-describedby={view.guidanceId}
            title={keybind.bindTitle('region')}
            onclick={() => keybind.saveTrigger('region')}
            disabled={view.bindDisabled}
          >
            {@render triggerIcon('region')}
          </button>
        </span>
      </div>
    </li>
  </ol>

  <p class="hintline">
    {view.hintLine}
  </p>

  {#if view.noShortcutDialog && view.guidanceText}
    <p id={GUIDANCE_ID} class="guidance" role="status">{view.guidanceText}</p>
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
      {#if view.portalBackend}
        <p>
          These triggers are set in your desktop's own shortcut settings, not in
          GOaT.
          {#if view.noShortcutDialog}
            This desktop has no dialog for the buttons to open, so they are off
            until a trigger is bound there.
          {:else}
            The buttons open that dialog.
          {/if}
          On KDE Plasma: System Settings → Keyboard → Shortcuts. On GNOME: Settings
          → Keyboard → View and Customize Shortcuts.
        </p>
      {/if}
      {#if view.status}
        <p class="hotkeyline" data-backend={view.status.backend}>
          {view.status.detail}
        </p>
        {#if view.status.warning}
          <p class="statusline" data-tone="warning" role="status">{view.status.warning}</p>
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
