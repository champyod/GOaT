<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { invoke } from '@tauri-apps/api/core';
  import { listen, type EventName, type UnlistenFn } from '@tauri-apps/api/event';
  import {
    getCurrentWindow,
    LogicalPosition,
    LogicalSize,
  } from '@tauri-apps/api/window';
  import { WebviewWindow } from '@tauri-apps/api/webviewWindow';
  import Camera from '@lucide/svelte/icons/camera';
  import ScanLine from '@lucide/svelte/icons/scan-line';
  import Menu from '@lucide/svelte/icons/menu';
  import X from '@lucide/svelte/icons/x';
  import CopyIcon from '@jis3r/icons/icons/copy';
  import CheckIcon from '@jis3r/icons/icons/check';

  const COPIED_FEEDBACK_MS = 1600;
  const ICON_SIZE = 16;
  const BAR_HEIGHT = 48;
  const BAR_TOP_OFFSET = 28;
  /// The menu is a window of its own, and these two are where it is put: its
  /// width is the bar's, so hanging it under the bar's right edge needs no
  /// reading of the bar, and the gap is what keeps it off the bar's own edge.
  const MENU_LABEL = 'menu';
  const MENU_WIDTH = 300;
  const MENU_GAP = 8;
  const BODY_WIDTH = 800;
  const BODY_HEIGHT = 600;
  /// The expanded view is fitted once its layout has stopped moving, so a burst
  /// of reflows out of one change is one window resize rather than one per frame.
  const FIT_DEBOUNCE_MS = 100;
  /// The window is only asked to move past this much difference on the leg that
  /// is fitted. A fit that lands within it of the height already asked for is
  /// the same layout measuring itself a fraction differently, and answering that
  /// would walk the window toward the clamp a little at a time.
  const FIT_EPSILON_PX = 2;
  const APPEARANCE_TINT_MAX = 100;
  const HEX_COLOR = /^#[\da-f]{6}$/i;
  /// The sentences the status bar can hold. They are named because a sentence
  /// typed at each of the places that showed one is a sentence the next reader
  /// spells differently, and a status bar carrying two of them at once is a bar
  /// nobody is reading.
  const READY = 'Ready';
  const CAPTURING = 'Capturing...';
  const CAPTURING_REGION = 'Capturing region...';
  const READING_REGION = 'Reading region...';
  const NO_TEXT = 'No text detected';
  const CAPTURE_INCOMPLETE = 'Capture incomplete';
  const SELECTION_TOO_SMALL = 'Selection too small';
  const NEEDS_SCREENSHOT =
    'Capture a screenshot first, then drag on it to select a region';
  /// The look the window draws before the stored appearance answers, and the one
  /// it keeps if the answer never comes, so neither a slow load nor a failed one
  /// is something the user sees. The menu is the window the accent is picked in,
  /// and a System accent takes the variable away again, which is what leaves the
  /// desktop's own colour in charge here too.
  const FALLBACK_TINT_PERCENT = 72;

  type AppearanceTheme = 'system' | 'dark' | 'light';

  type Appearance = {
    accent: string | null;
    blur_px: number;
    tint_opacity: number;
    theme: AppearanceTheme;
  };

  type CapturedImage = {
    width: number;
    height: number;
    rgba: number[];
  };

  type MonitorInfo = {
    index: number;
    name: string;
    is_primary: boolean;
    width: number;
    height: number;
    x: number;
    y: number;
  };

  type ResultPayload = {
    image: CapturedImage;
    ocr_text: string;
    translated_text: string;
    /// Named by the backend and part of what it sends, so the type still mirrors
    /// the payload even though nothing on this page reads it.
    ocr_engine: string;
    error: string;
  };

  /// Where a capture is. One value rather than a set of flags, so a step the
  /// backend has not reached cannot be held at the same time as one it has: the
  /// covers over the two fields and the answerability of the bar are all read
  /// off this, and they were three booleans free to disagree with each other.
  type Phase =
    | 'idle'
    | 'capturing'
    | 'reading'
    | 'translating'
    | 'done'
    | 'error';

  /// The phases the backend publishes. `idle` and `capturing` belong to this
  /// page: nothing is in flight before a run starts, and a window that starts
  /// one knows it is capturing before the backend has said anything at all.
  type BackendPhase = 'reading' | 'translating' | 'done' | 'error';

  /// The one message a capture arrives as. A field the step does not have is
  /// absent rather than empty, so a step that read nothing is not read as a
  /// step whose text is blank.
  type CaptureProgress = {
    phase: BackendPhase;
    image?: CapturedImage;
    ocr_text?: string;
    payload?: ResultPayload;
    error?: string;
  };

  /// How a line on the status bar is to be read: a step of a run, something the
  /// user has to act on, or something that went wrong. It is written with the
  /// sentence instead of derived from it, so the two cannot disagree.
  type StatusTone = 'info' | 'warning' | 'error';

  type ModelsStatus = {
    ready: boolean;
  };

  let canvasEl: HTMLCanvasElement | undefined = $state();
  let phase = $state<Phase>('idle');
  let statusNote = $state(READY);
  let statusTone = $state<StatusTone>('info');
  let ocrText = $state('');
  let translatedText = $state('');
  let hasImage = $state(false);
  let monitors = $state<MonitorInfo[]>([]);
  let monitor = $state(0);
  let accent = $state<string | null>(null);
  let tintOpacity = $state(FALLBACK_TINT_PERCENT);
  let theme = $state<AppearanceTheme>('system');
  let selecting = $state(false);
  let screenMode = $state(false);
  let copiedOcr = $state(false);
  let copiedTranslated = $state(false);
  let selStart = $state<{ x: number; y: number } | null>(null);
  let selRect = $state<{ x: number; y: number; w: number; h: number } | null>(
    null
  );
  let imgSize = $state<{ width: number; height: number } | null>(null);
  let selOffset = $state({ x: 0, y: 0 });
  let copiedTimer: ReturnType<typeof setTimeout> | undefined;
  let fitTimer: ReturnType<typeof setTimeout> | undefined;
  /// The size the window was last asked to take. The height is the one the
  /// expanded view is measured against, because the window is what the
  /// measurement moves. The width is never measured: it is held at whatever the
  /// first fit asked for and sent back unchanged, so no fit is ever answered
  /// with its own result.
  let lastFitSize: { width: number; height: number } | null = null;
  let bodyEl: HTMLDivElement | undefined = $state();
  let contentEl: HTMLDivElement | undefined = $state();
  let hasGrown = false;
  let barPlaced = false;
  const registered: UnlistenFn[] = [];

  /// The three things the phase is asked for rather than kept. The screenshot is
  /// handed over before the backend has read it, so a read can be under way with
  /// no call of this page's own to mark it: a cover is up exactly while its own
  /// phase is running, and comes off the moment that phase ends.
  const isBusy = $derived(
    phase === 'capturing' || phase === 'reading' || phase === 'translating'
  );
  const awaitingResult = $derived(phase === 'capturing' || phase === 'reading');
  const awaitingTranslate = $derived(phase === 'translating');

  /// The sentences the phase is announced with, keyed by the phase they name, so
  /// a phase this page can be in and a sentence it says for it cannot drift
  /// apart: adding a phase without a line here is a type error. `idle` has none
  /// because there is nothing to say about a window that is not working on
  /// anything, and an empty line in a live region is read out as silence.
  const PHASE_SENTENCES: Record<Phase, string> = {
    idle: '',
    capturing: 'Capturing the screen.',
    reading: 'Reading text from the screenshot.',
    translating: 'Translating the text that was read.',
    done: 'Capture complete.',
    error: 'The capture failed.',
  };
  /// One region reads this out for the whole run. The covers over the fields
  /// used to carry a `role="status"` each, which announced the same run twice —
  /// once per field — and only for as long as a cover was up: a phase with
  /// nothing to cover it, and the failure that ends a run with the covers already
  /// off, were both silent. The covers are now hidden from a screen reader and
  /// the phase says all of it, in one place, for as long as it is true.
  const phaseSentence = $derived(PHASE_SENTENCES[phase]);

  /// The diagnostics window is the one place a problem is written down, so a
  /// failure here is handed to it instead of being painted over the screenshot.
  /// `console` is the only channel left if the handover itself does not get
  /// through, and a silent failure would be the one thing worse than a noisy
  /// overlay.
  function reportError(source: string, message: string): void {
    void invoke('report_frontend_error', { source, message }).catch(
      (e: unknown) => {
        console.error(`GOaT could not record: ${message}`, e);
      }
    );
  }

  /// A registration that fails hands back no unlisten function, so a rejected
  /// `listen` would be an unhandled rejection and the teardown could never wait
  /// on it. Settling it once here keeps a failure in the diagnostics window and
  /// a success available for the teardown.
  function subscribe<T>(
    event: EventName,
    handler: (payload: T) => void
  ): Promise<void> {
    return listen<T>(event, (e) => handler(e.payload))
      .then((unlisten) => {
        registered.push(unlisten);
      })
      .catch((e: unknown) => {
        reportError('event', `the ${event} feed could not be opened: ${String(e)}`);
      });
  }

  /// Paints the frame. A canvas that is not in the document, or a context the
  /// page will not be given, is a run that cannot be shown rather than one that
  /// can be shown as an empty box: the throw is what turns it into a failure with
  /// a reason on the status bar, instead of a settled run over a blank canvas
  /// and a status line claiming the text is there.
  function draw(image: CapturedImage): void {
    if (!canvasEl) {
      throw new Error('the canvas the screenshot is drawn on is not on the page');
    }
    canvasEl.width = image.width;
    canvasEl.height = image.height;
    const ctx = canvasEl.getContext('2d');
    if (!ctx) {
      throw new Error('the screenshot canvas would not give a drawing context');
    }
    const imageData = new ImageData(
      new Uint8ClampedArray(image.rgba),
      image.width,
      image.height
    );
    ctx.putImageData(imageData, 0, 0);
    hasImage = true;
  }

  /// The window starts as a bar and the first capture is what fills it, so the
  /// one moment it can take the body size is the moment the body first appears.
  /// A later capture leaves the window alone, or it would undo a size the user
  /// chose for themselves.
  ///
  /// That same moment gives the desktop's own title bar back, because a window
  /// that is now a whole app rather than a strip is one the title bar belongs
  /// on. A bar-only launch never reaches here, so it stays frameless.
  $effect(() => {
    if (!hasImage || hasGrown) return;
    hasGrown = true;
    const win = getCurrentWindow();
    void win.setDecorations(true).catch((e: unknown) => {
      reportError('window', `the title bar could not be restored: ${String(e)}`);
    });
    void win
      .setSize(new LogicalSize(BODY_WIDTH, BODY_HEIGHT))
      .catch((e: unknown) => {
        reportError('window', `the window could not be resized: ${String(e)}`);
      });
  });

  /// The body and its canvas are behind `hasImage`, so a result that arrives
  /// before the first one would find no canvas to paint. The flag goes up
  /// first and the flush that follows is what puts the canvas in the document,
  /// so every entry point into `draw` paints the frame it was handed.
  ///
  /// The overlay over the fields comes down here rather than with the caller, so
  /// it lifts together with the text it was covering. A throw below it — an
  /// image the canvas will not take, say — is a run that failed and not a
  /// rejection the window has no handler for, because the result can arrive from
  /// a trigger rather than from a call this page made and is waiting on.
  async function applyResult(
    result: ResultPayload,
    origin: { x: number; y: number } | null = null
  ): Promise<void> {
    try {
      hasImage = true;
      await tick();
      draw(result.image);
      imgSize = { width: result.image.width, height: result.image.height };
      // A trigger publishes the same result as a button does and knows no offset
      // for it, so this is a whole screen unless the caller that started the run
      // says otherwise. That caller applies the result again when its own call
      // answers, and does so with the offset it was given, which is what leaves
      // a selection read pointing back into the screenshot it was cut from.
      selOffset = origin ?? { x: 0, y: 0 };
      ocrText = result.ocr_text;
      translatedText = result.translated_text;
      if (!ocrText.trim()) {
        note(
          result.error ? CAPTURE_INCOMPLETE : NO_TEXT,
          result.error ? 'warning' : 'info'
        );
      } else {
        note('', 'info');
      }
    } catch (e) {
      fail('capture', String(e));
      return;
    }
    settle('done');
  }

  /// One line on the status bar, written with its tone, so a sentence and the
  /// way it is to be read are never set apart.
  function note(text: string, tone: StatusTone): void {
    statusNote = text;
    statusTone = tone;
  }

  /// The one exit every capture run takes. A result applied, an `invoke` that
  /// rejected and a failure the backend published all end here, so the fields
  /// are never left covered over and the bar is never left holding a run that is
  /// over.
  function settle(outcome: 'done' | 'error'): void {
    phase = outcome;
  }

  /// A run that failed, whatever told this page so: a rejected call of its own
  /// or the terminal step of the backend's account. The reason is shown rather
  /// than restated, because the backend knows what went wrong and a line saying
  /// only that the capture failed has thrown that answer away on the way to the
  /// one person who can act on it.
  function fail(source: string, reason: string): void {
    reportError(source, reason);
    note(reason, 'error');
    settle('error');
  }

  /// The one place a capture is started. The two callers that used to carry their
  /// own copy of the block below differ only in the command they ask for, the
  /// arguments it takes, the line the status bar holds while it runs and, for a
  /// selection read, where in the full screenshot it is reading from — so that
  /// is all they pass. The result is awaited before the run is settled, which
  /// is what keeps the covers up over the fields the result is about to fill.
  async function runCapture(
    command: string,
    args: Record<string, unknown> | undefined,
    source: string,
    pendingNote: string,
    origin: { x: number; y: number } | null = null
  ): Promise<void> {
    if (isBusy) return;
    phase = 'capturing';
    note(pendingNote, 'info');
    try {
      const result = await invoke<ResultPayload>(command, args);
      await applyResult(result, origin);
    } catch (e) {
      fail(source, String(e));
    }
  }

  async function capture(): Promise<void> {
    await runCapture('capture_primary', undefined, 'capture', CAPTURING);
  }

  /// The screenshot of a hotkey capture arrives with no result behind it, and it
  /// is the first thing that can fill an empty window, so it waits for the same
  /// canvas the result path waits for. It arrives on the step that says the read
  /// is under way, so the fields are covered from here on whether the run was
  /// started by this page or by a trigger.
  async function drawImage(image: CapturedImage): Promise<void> {
    hasImage = true;
    await tick();
    try {
      draw(image);
    } catch (e) {
      fail('capture', String(e));
      return;
    }
    imgSize = { width: image.width, height: image.height };
    selOffset = { x: 0, y: 0 };
  }

  /// The whole of a capture, as the one message the backend sends it on. The
  /// phase is set before the picture or the text that goes under the covers
  /// arrives, so a cover is never up after what it was covering has landed.
  function onCaptureProgress(progress: CaptureProgress): void {
    switch (progress.phase) {
      case 'reading':
        phase = 'reading';
        if (progress.image) void drawImage(progress.image);
        return;
      case 'translating':
        phase = 'translating';
        if (typeof progress.ocr_text === 'string') ocrText = progress.ocr_text;
        return;
      case 'done':
        if (progress.payload) void applyResult(progress.payload);
        return;
      case 'error':
        fail('capture', progress.error ?? 'Capture failed');
        return;
    }
  }

  onMount(() => {
    void subscribe<CaptureProgress>('capture-progress', onCaptureProgress);
    void subscribe('region-select', () => {
      if (monitors.length === 0) {
        reportError('monitor', 'no monitor info is available');
        return;
      }
      selStart = null;
      selRect = null;
      selecting = true;
      screenMode = true;
      window.addEventListener('keydown', cancelSelectOnEsc);
    });
    invoke<ModelsStatus>('models_status')
      .then((value) => {
        if (value.ready) {
          return invoke<string>('init_models');
        }
        return null;
      })
      .then((message) => {
        if (message) note(message, 'info');
      })
      .catch((e) => {
        reportError('models', String(e));
      });
    invoke<MonitorInfo[]>('list_monitors')
      .then((value) => {
        monitors = value;
        void placeBarAtTop(value);
      })
      .catch((e) => {
        reportError('monitor', String(e));
      });
    invoke<number>('get_monitor')
      .then((value) => {
        monitor = value;
      })
      .catch((e) => {
        reportError('monitor', String(e));
      });
    void subscribe<Appearance>('appearance-changed', applyAppearance);
    invoke<Appearance>('get_appearance')
      .then(applyAppearance)
      .catch((e: unknown) => {
        reportError('appearance', String(e));
      });
    return () => {
      for (const unlisten of registered) unlisten();
      if (copiedTimer) clearTimeout(copiedTimer);
    };
  });

  /// A colour the styles cannot read is a string the file happens to hold, not
  /// one the window can draw, and the file is editable by hand — so anything
  /// that is not a six-digit hex colour is taken as the desktop's own.
  function asAccent(value: string | null): string | null {
    return value !== null && HEX_COLOR.test(value) ? value : null;
  }

  /// Both sliders are whole numbers inside the range the menu offers. A file
  /// written by anything other than this menu is the only way a number arrives
  /// that is not one, and `blur(NaNpx)` would void the declaration it stands in
  /// rather than draw a weaker blur.
  function asSliderValue(value: number, max: number): number {
    if (!Number.isFinite(value)) return 0;
    return Math.min(Math.max(Math.round(value), 0), max);
  }

  function asTheme(value: string): AppearanceTheme {
    return value === 'dark' || value === 'light' ? value : 'system';
  }

  /// The look reaches the window three ways — the load on mount, a press in the
  /// menu window, and a write from that window arriving as an event — and all
  /// three come through here, so the bar and the body are never two different
  /// appearances and a value is checked before it reaches either.
  function applyAppearance(loaded: Appearance): void {
    accent = asAccent(loaded.accent);
    tintOpacity = asSliderValue(loaded.tint_opacity, APPEARANCE_TINT_MAX);
    theme = asTheme(loaded.theme);
  }

  /// The look is two variables and a colour scheme on the document element, so
  /// every rule that reads them repaints without this page re-rendering. The
  /// accent is checked once more on the way out: a value `color-mix` cannot
  /// read voids the whole declaration it stands in, taking the dark fallback
  /// line with it, so the variable is taken away instead and the stylesheet's
  /// own fallbacks keep the window the shape it has.
  ///
  /// System writes nothing at all, because a tree with no `color-scheme` of its
  /// own is what lets the desktop's own popup and scrollbar colours through —
  /// but this page's `color-scheme: dark` is there to stop that popup coming up
  /// white on a dark panel, so removing the inline value hands the decision back
  /// to the stylesheet rather than to the desktop.
  $effect(() => {
    const root = document.documentElement;
    const chosen = asAccent(accent);
    if (chosen) {
      root.style.setProperty('--goat-accent', chosen);
    } else {
      root.style.removeProperty('--goat-accent');
    }
    const tint = asSliderValue(tintOpacity, APPEARANCE_TINT_MAX);
    root.style.setProperty('--goat-tint', `${tint}%`);
    if (theme === 'system') {
      root.style.removeProperty('color-scheme');
    } else {
      root.style.setProperty('color-scheme', theme);
    }
  });

  /// The bar is meant to sit along the top of the primary monitor, centred, and
  /// the window otherwise comes up wherever the desktop put it. Monitor geometry
  /// arrives in physical pixels while `setPosition` takes logical ones, so every
  /// distance goes through the window's own scale factor, the same way the menu
  /// grow reads its width back.
  ///
  /// The compositor owns placement where it is allowed to, and on Wayland the
  /// client is not: there the move is ignored and the desktop puts the bar
  /// wherever its rules say, which is why a KDE user pins it to the top with a
  /// Window Rule rather than with anything in here. The call is therefore an
  /// enhancement and never a guarantee — it holds the intended placement on X11,
  /// Windows and macOS, and a Wayland session simply takes the no-op path.
  ///
  /// The move happens once: a bar the user has since dragged somewhere is theirs,
  /// and re-placing it would take it away again.
  async function placeBarAtTop(list: MonitorInfo[]): Promise<void> {
    if (barPlaced) return;
    barPlaced = true;
    const win = getCurrentWindow();
    try {
      const [size, scale] = await Promise.all([
        win.outerSize(),
        win.scaleFactor(),
      ]);
      const primary = list.find((m) => m.is_primary) ?? list[0];
      const monitorWidth = primary ? primary.width / scale : window.screen.width;
      const monitorLeft = primary ? primary.x / scale : 0;
      const barWidth = size.toLogical(scale).width;
      const x = Math.round(monitorLeft + (monitorWidth - barWidth) / 2);
      await win.setPosition(new LogicalPosition(x, BAR_TOP_OFFSET));
    } catch (e) {
      reportError('window', `the bar could not be placed: ${String(e)}`);
    }
  }

  /// The menu is a window of its own, so the button only decides where it goes:
  /// under the bar's right edge, the side the button is on, a gap below the bar
  /// so the two surfaces are not flush. The bar's outer position and size arrive
  /// in physical pixels while `setPosition` takes logical ones, so every
  /// distance goes through the window's own scale factor the same way
  /// `placeBarAtTop` divides its monitor geometry.
  ///
  /// A second press hides the window rather than opening it again, so the button
  /// is its own answer: the window shows and takes focus, and the press that
  /// loses the focus is the one that closes it, whichever window the user
  /// pressed on.
  async function toggleMenu(): Promise<void> {
    const win = getCurrentWindow();
    try {
      const menu = await WebviewWindow.getByLabel(MENU_LABEL);
      if (!menu) {
        reportError('window', 'the menu window is not there');
        return;
      }
      if (await menu.isVisible()) {
        await menu.hide();
        return;
      }
      const [position, size, scale] = await Promise.all([
        win.outerPosition(),
        win.outerSize(),
        win.scaleFactor(),
      ]);
      const origin = position.toLogical(scale);
      const x = origin.x + size.toLogical(scale).width - MENU_WIDTH;
      const y = origin.y + BAR_HEIGHT + MENU_GAP;
      await menu.setPosition(new LogicalPosition(Math.round(x), Math.round(y)));
      await menu.show();
      await menu.setFocus();
    } catch (e) {
      reportError('window', `the menu could not be opened: ${String(e)}`);
    }
  }

  /// The height the window has to be for the expanded view to fit whole, or null
  /// when there is nothing laid out to measure. The body is stretched to the
  /// window, so its own box is the window read back and says nothing about the
  /// room the content wants; the content grid is the one box in that column laid
  /// out at its natural size, and everything from the top of the document down
  /// to the padding under it is inside the window. Offsets stand in for a client
  /// rect because they read the same whether or not the view is scrolled.
  function expandedContentHeight(): number | null {
    if (!bodyEl || !contentEl) return null;
    const paddingBelow = parseFloat(getComputedStyle(bodyEl).paddingBottom);
    const height = contentEl.offsetTop + contentEl.offsetHeight + paddingBelow;
    if (!Number.isFinite(height)) return null;
    // A read taken before the view has been laid out comes back as nothing, and
    // asking for a window of nothing is how a fit ends up at the clamp rather
    // than at the content.
    if (height <= 0) return null;
    return height;
  }

  /// The height is fitted and the width is sent back exactly as it was last
  /// asked for. Fitting the width would measure the body, and the body is the
  /// window: a fit that reaches the height clamp brings a scrollbar with it, the
  /// body loses the width of that scrollbar, and the narrower window handed back
  /// for it is that same walk one step on. The desktop clamps whatever width it
  /// is given, so the frozen one is safe to keep sending.
  ///
  /// A refusal is written down rather than raised: the view works at whatever
  /// size it has, so a window the desktop will not take is a report and not a
  /// failure. The size asked for is kept either way, so a layout that keeps
  /// reading the same does not keep asking.
  async function fitWindowToContent(height: number): Promise<void> {
    const width = lastFitSize?.width ?? bodyEl?.offsetWidth;
    if (width === undefined || width <= 0) return;
    lastFitSize = { width, height };
    try {
      await invoke('set_window_size', { width, height });
    } catch (e) {
      reportError('window', `the window could not be fitted: ${String(e)}`);
    }
  }

  /// One fit per settled layout, not one per observation: a capture, a reflow
  /// and a scrollbar appearing all arrive as several sizes of the same view, and
  /// each of them would otherwise be a window the user watches move. Only the
  /// height is compared, because the width is sent back as it was and so cannot
  /// have moved.
  function scheduleWindowFit(): void {
    if (fitTimer) clearTimeout(fitTimer);
    fitTimer = setTimeout(() => {
      fitTimer = undefined;
      const height = expandedContentHeight();
      if (!height) return;
      const asked = lastFitSize;
      if (asked && Math.abs(height - asked.height) <= FIT_EPSILON_PX) return;
      void fitWindowToContent(height);
    }, FIT_DEBOUNCE_MS);
  }

  /// The expanded view is watched for exactly as long as it is on screen, and
  /// only while it is: the bar-only window holds no content to measure, and a
  /// menu that is a window of its own no longer sizes this one.
  $effect(() => {
    const target = hasImage ? contentEl : undefined;
    if (!target) return;
    const observer = new ResizeObserver(() => scheduleWindowFit());
    observer.observe(target);
    return () => {
      observer.disconnect();
      if (fitTimer) {
        clearTimeout(fitTimer);
        fitTimer = undefined;
      }
    };
  });

  /// The bar is the title bar, so closing it hides the window instead of ending
  /// the process: the hotkeys are registered outside the window and a quit
  /// would take them with it.
  async function closeToTray(): Promise<void> {
    try {
      await invoke('hide_window');
    } catch (e) {
      reportError('window', `the window could not be hidden: ${String(e)}`);
    }
  }

  /// Both panels share one timer so a second copy restarts the feedback rather
  /// than leaving the first one to clear an icon that already moved on.
  function flashCopied(field: 'ocr' | 'translated'): void {
    if (field === 'ocr') copiedOcr = true;
    else copiedTranslated = true;
    if (copiedTimer) clearTimeout(copiedTimer);
    copiedTimer = setTimeout(() => {
      copiedOcr = false;
      copiedTranslated = false;
    }, COPIED_FEEDBACK_MS);
  }

  async function copyText(
    text: string,
    field: 'ocr' | 'translated'
  ): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      flashCopied(field);
    } catch (e) {
      reportError('clipboard', String(e));
    }
  }

  function startSelect(): void {
    if (!hasImage) {
      note(NEEDS_SCREENSHOT, 'warning');
      return;
    }
    selStart = null;
    selRect = null;
    selecting = true;
    window.addEventListener('keydown', cancelSelectOnEsc);
  }

  /// Ends selection and takes the overlay off the screen, so a caller that is
  /// about to grab a region can wait for that to be off the screen first. The
  /// fullscreen request belongs to that and not to the caller: a refused one is
  /// a refused teardown, which is reported here so it cannot abort the grab the
  /// caller makes next.
  async function stopSelectMode(): Promise<void> {
    const wasScreen = screenMode;
    selecting = false;
    screenMode = false;
    selStart = null;
    selRect = null;
    window.removeEventListener('keydown', cancelSelectOnEsc);
    if (!wasScreen) return;
    try {
      const win = getCurrentWindow();
      if (await win.isFullscreen()) {
        await win.setFullscreen(false);
      }
    } catch (e) {
      reportError('window', String(e));
    }
  }

  function cancelSelectOnEsc(event: KeyboardEvent) {
    if (event.key === 'Escape') {
      void stopSelectMode();
    }
  }

  function canvasPos(event: MouseEvent) {
    const wrap = event.currentTarget as HTMLDivElement;
    const box = wrap.getBoundingClientRect();
    return { x: event.clientX - box.left, y: event.clientY - box.top };
  }

  function onSelDown(event: MouseEvent) {
    if (!selecting) return;
    let p: { x: number; y: number };
    if (screenMode) {
      p = { x: event.clientX, y: event.clientY };
    } else {
      p = canvasPos(event);
    }
    selStart = p;
    selRect = { x: p.x, y: p.y, w: 0, h: 0 };
  }

  function onSelMove(event: MouseEvent) {
    if (!selecting || !selStart) return;
    let cx: number;
    let cy: number;
    let maxW: number;
    let maxH: number;
    if (screenMode) {
      cx = event.clientX;
      cy = event.clientY;
      maxW = window.innerWidth;
      maxH = window.innerHeight;
    } else {
      if (!canvasEl) return;
      const box = canvasEl.getBoundingClientRect();
      cx = event.clientX - box.left;
      cy = event.clientY - box.top;
      maxW = box.width;
      maxH = box.height;
    }
    const x = Math.max(0, Math.min(selStart.x, cx));
    const y = Math.max(0, Math.min(selStart.y, cy));
    selRect = {
      x,
      y,
      w: Math.max(0, Math.min(cx, maxW) - x),
      h: Math.max(0, Math.min(cy, maxH) - y),
    };
  }

  async function finishScreenSelect(): Promise<void> {
    const rect = selRect;
    const mon = monitors.find((m) => m.index === monitor) ?? monitors[0];
    const scale = await getCurrentWindow().scaleFactor();
    const tooSmall = !rect || rect.w < 4 || rect.h < 4;
    await stopSelectMode();
    if (tooSmall || !rect || !mon) {
      if (!mon) reportError('monitor', 'no monitor info is available');
      else note(SELECTION_TOO_SMALL, 'warning');
      return;
    }
    const x = Math.max(0, Math.round(rect.x * scale + mon.x));
    const y = Math.max(0, Math.round(rect.y * scale + mon.y));
    const width = Math.max(1, Math.round(rect.w * scale));
    const height = Math.max(1, Math.round(rect.h * scale));
    await runCapture(
      'capture_region',
      { monitor, x, y, width, height },
      'capture',
      CAPTURING_REGION
    );
  }

  async function onSelUp(): Promise<void> {
    if (!selecting || !selRect || isBusy) {
      return;
    }
    if (screenMode) {
      await finishScreenSelect();
      return;
    }
    if (!canvasEl || !imgSize) {
      return;
    }
    const box = canvasEl.getBoundingClientRect();
    const scaleX = imgSize.width / box.width;
    const scaleY = imgSize.height / box.height;
    const x = Math.min(
      imgSize.width - 1,
      Math.max(0, Math.round(selRect.x * scaleX))
    );
    const y = Math.min(
      imgSize.height - 1,
      Math.max(0, Math.round(selRect.y * scaleY))
    );
    const width = Math.max(
      1,
      Math.min(Math.round(selRect.w * scaleX), imgSize.width - x)
    );
    const height = Math.max(
      1,
      Math.min(Math.round(selRect.h * scaleY), imgSize.height - y)
    );
    const fullX = selOffset.x + x;
    const fullY = selOffset.y + y;
    const tooSmall = selRect.w < 4 || selRect.h < 4;
    await stopSelectMode();
    if (tooSmall) {
      note(SELECTION_TOO_SMALL, 'warning');
      return;
    }
    await runCapture(
      'ocr_selection',
      { x: fullX, y: fullY, width, height },
      'ocr',
      READING_REGION,
      { x: fullX, y: fullY }
    );
  }
</script>

<main class:has-body={hasImage}>
  <!-- The one place a run is announced. It is outside every branch below so it
       is on the page through the whole of a run rather than only while a cover
       happens to be up, and it is read politely so it never cuts across what the
       user is already on. The covers over the fields are hidden from it and
       take their text from the phase, which is what stops a single read being
       announced twice — once per field. -->
  <p class="visually-hidden" aria-live="polite">{phaseSentence}</p>

  {#if selecting && screenMode}
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions --
      Full-screen drag surface; Esc to cancel is on window keydown. -->
    <div
      class="overlay"
      role="application"
      aria-label="Drag to select a screen region"
      onmousedown={onSelDown}
      onmousemove={onSelMove}
      onmouseup={onSelUp}
    >
      <p class="overlay-hint">Drag to select a region. Esc cancels.</p>
      {#if selRect}
        <div
          class="selrect screen"
          style="left: {selRect.x}px; top: {selRect.y}px; width: {selRect.w}px; height: {selRect.h}px;"
        ></div>
      {/if}
    </div>
  {:else}
    <div class="bar">
      <!-- Order contract: capture, region, grip, menu, close. The grip stays
           between the leading actions and the trailing ones, so a press on a
           button is never read as a window drag. -->
      <div class="actions">
        <button
          class="icon"
          onclick={capture}
          disabled={isBusy}
          aria-label="Capture"
          title="Capture"
        >
          <Camera size={ICON_SIZE} />
        </button>
        <button
          class="icon"
          onclick={startSelect}
          disabled={isBusy || !hasImage}
          aria-label="Select region"
          title={hasImage ? 'Select region' : 'Capture a screenshot first'}
        >
          <ScanLine size={ICON_SIZE} />
        </button>
      </div>
      <div class="grip" data-tauri-drag-region aria-hidden="true"></div>
      <div class="actions trailing">
        <button
          class="icon"
          onclick={toggleMenu}
          aria-label="Menu"
          title="Menu"
        >
          <Menu size={ICON_SIZE} />
        </button>
        <button
          class="icon ghost"
          onclick={closeToTray}
          aria-label="Close"
          title="Close (hide to tray)"
        >
          <X size={ICON_SIZE} />
        </button>
      </div>
    </div>

    {#if hasImage}
    <div class="body" bind:this={bodyEl}>
      {#if statusNote}
        <p class="status" data-tone={statusTone} role="status">{statusNote}</p>
      {/if}

      <div class="content" bind:this={contentEl}>
        <section class="shot">
          <h2>Screenshot</h2>
          <!-- svelte-ignore a11y_no_noninteractive_element_interactions --
            Drag surface over the screenshot; Esc to cancel is on window keydown. -->
          <div
            class="shotwrap"
            class:armed={selecting}
            role="application"
            aria-label="Drag on the screenshot to select a region"
            onmousedown={onSelDown}
            onmousemove={onSelMove}
            onmouseup={onSelUp}
          >
            <canvas bind:this={canvasEl}></canvas>
            {#if selRect}
              <div
                class="selrect"
                style="left: {selRect.x}px; top: {selRect.y}px; width: {selRect.w}px; height: {selRect.h}px;"
              ></div>
            {/if}
          </div>
          {#if selecting}
            <p class="placeholder">Drag to select. Esc cancels.</p>
          {/if}
        </section>

        <div class="fields" aria-busy={awaitingResult || awaitingTranslate}>
          <section>
            <div class="head">
              <h2>OCR'ed text</h2>
              <button
                class="icon"
                onclick={() => copyText(ocrText, 'ocr')}
                disabled={!ocrText}
                aria-label="Copy OCR'ed text"
                title="Copy"
              >
                {#if copiedOcr}
                  <CheckIcon size={ICON_SIZE} animate={true} />
                {:else}
                  <CopyIcon size={ICON_SIZE} animate={false} />
                {/if}
              </button>
            </div>
            <textarea bind:value={ocrText} placeholder="Empty" rows={6}
            ></textarea>
            {#if awaitingResult}
              <div class="field-loading" aria-hidden="true">
                <span class="field-spinner"></span>
                <span>Reading…</span>
              </div>
            {/if}
          </section>
          <section>
            <div class="head">
              <h2>Translated text</h2>
              <button
                class="icon"
                onclick={() => copyText(translatedText, 'translated')}
                disabled={!translatedText}
                aria-label="Copy translated text"
                title="Copy"
              >
                {#if copiedTranslated}
                  <CheckIcon size={ICON_SIZE} animate={true} />
                {:else}
                  <CopyIcon size={ICON_SIZE} animate={false} />
                {/if}
              </button>
            </div>
            <textarea
              bind:value={translatedText}
              placeholder="Empty"
              rows={6}
            ></textarea>
            {#if awaitingTranslate}
              <div class="field-loading" aria-hidden="true">
                <span class="field-spinner"></span>
                <span>Translating…</span>
              </div>
            {/if}
          </section>
        </div>
      </div>
    </div>
    {/if}
  {/if}
</main>

<style>
  /* A native dropdown popup is drawn by the desktop rather than by this page, so
     the page tells the whole tree it is dark and the option list below carries
     its own colours, or the popup can come up white on a white window. */
  :global(html) {
    color-scheme: dark;
  }

  /* The window's look lives in variables on the document element, and its
     fallbacks sit in the rules that read them rather than here, because the
     strip and the body have always drawn at different blur strengths and one
     shared fallback would flatten that. Those fallbacks are the page before the
     stored appearance answers, and the page if it never comes. A chosen accent
     is written over the one below on the document element, and a System accent
     takes that variable away again, which is what leaves the desktop's own
     colour in charge. */
  :global(:root) {
    --goat-accent: AccentColor;
    --goat-tint: 72%;
  }

  :global(body) {
    margin: 0;
    padding: 0;
    background: transparent;
    font-family: system-ui, sans-serif;
    scrollbar-color: rgba(255, 255, 255, 0.3) transparent;
  }

  :global(::-webkit-scrollbar) {
    width: 10px;
    height: 10px;
    background: transparent;
  }

  :global(::-webkit-scrollbar-track) {
    background: transparent;
    border: none;
  }

  :global(::-webkit-scrollbar-thumb) {
    background: rgba(255, 255, 255, 0.25);
    border: 3px solid transparent;
    background-clip: content-box;
    border-radius: 8px;
  }

  :global(::-webkit-scrollbar-corner) {
    background: transparent;
  }

  main {
    position: relative;
    display: flex;
    flex-direction: column;
    color: #fff;
  }

  /* The window is only tall enough for the bar until a capture fills it, so the
     wash is scoped to the body and the bar sits on transparency instead of
     stretching over the rest of the window. Once the column is painted the
     window is a whole app, and a wash under the whole of it is what keeps the
     raw desktop out from behind the bar and around the body; the bar-only
     window keeps nothing behind it. */
  main.has-body {
    min-height: 100vh;
    background: rgba(10, 14, 22, 0.55);
  }

  /* The menu panel hangs below the bar and out of flow, so it makes the document
     taller than a bar-only window and hands that window a scrollbar it has no
     use for. The column is given the window's height first, because a panel
     positioned out of flow is clipped by the box it is positioned against rather
     than by the window, and the panel's own max-height already keeps whatever
     does not fit scrolling inside the panel. The expanded view is the one meant
     to scroll, so the rule is written as the other state; `clip` is declared
     after `hidden` so a WebKit without it keeps the clip it does understand,
     and the strip is the only thing inside this column, so the two answers
     differ in nothing that is on screen. */
  main:not(.has-body) {
    min-height: 100vh;
    overflow: hidden;
    overflow: clip;
    /* Frameless windows get no corner rounding from the desktop, so the bar
       carries the native radius itself; the clip above makes it real. Scoped
       to bar-only: expanded and overlay modes must stay square. */
    border-radius: 10px;
  }

  /* There is no title bar of the desktop's own, so this strip stands in for one.
     The window is transparent, so the strip carries a translucent tint and blurs
     whatever the desktop shows through it rather than sitting on an opaque plate,
     and it stays a fixed-height row, which leaves the remainder of the bar-only
     window transparent rather than painted.

     The tint is the desktop's own accent colour, so the strip belongs to the
     system the user chose rather than to this app. The dark line comes first on
     purpose: a WebKit that cannot resolve `color-mix` or `AccentColor` drops the
     second declaration and keeps a plain dark strip, which belongs to no palette
     at all rather than borrowing one the user never chose.

     The strip is not frosted on this window, so the tint is what keeps a
     button legible over whatever the desktop shows through it, and the blur
     behind it is there for a compositor that frosts. The top highlight is
     there for the same reason: without frosting, a glass edge is the only
     thing on the strip that reads as glass. */
  .bar {
    position: sticky;
    top: 0;
    z-index: 30;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.5rem;
    background: rgba(16, 24, 40, 0.72);
    background: color-mix(
      in srgb,
      var(--goat-accent) var(--goat-tint),
      transparent
    );
    border-top: 1px solid rgba(255, 255, 255, 0.14);
    border-bottom: 1px solid rgba(255, 255, 255, 0.12);
  }

  h2 {
    margin: 0;
    font-size: 0.95rem;
  }

  /* One container language for the whole strip. A button that carries its own
     wash next to another that carries the same one reads as a single lighter
     slab set into the bar rather than as segments of a control, so the group
     is the only shape and the buttons in it are the segments. */
  .actions {
    display: flex;
    gap: 0.25rem;
  }

  /* A group holding the close glyph keeps no pill: that glyph sits where the
     title bar's own button would, and a wash behind it would put it back inside
     a container it is meant to stand outside of. */
  .actions:not(:has(> .icon.ghost)) {
    gap: 2px;
    padding: 2px;
    background: rgba(255, 255, 255, 0.1);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 0.5rem;
  }

  .actions.trailing {
    margin-left: auto;
  }

  /* A segment is flat, so the pill is the only resting surface and hover is the
     only thing that ever raises one. */
  .actions > .icon {
    background: transparent;
    border: none;
    border-radius: 0.35rem;
  }

  /* A drag region around the buttons leaves a press on any of them answered by
     the window manager instead of the button, so the empty space between the
     groups is the only part of the bar that drags. */
  .grip {
    flex: 1;
    align-self: stretch;
    min-width: 1rem;
    cursor: grab;
  }

  /* The body is the one place the window is a whole app, so it is the one place
     the desktop is allowed to show through: a wash far thinner than the bar and
     a blur that keeps the text legible over it. A fully transparent body would
     put white text straight onto whatever the user has behind, and an opaque one
     would throw away the reason the window was transparent to begin with. */
  .body {
    flex: 1;
    display: flex;
    flex-direction: column;
    box-sizing: border-box;
    padding: 1rem;
    background: rgba(255, 255, 255, 0.04);
  }

  /* On the page and out of sight: taken out of the layout rather than hidden,
     because `display: none` and `visibility: hidden` are both read by a screen
     reader as nothing being there, which is the one thing this line must not be
     while a run is under way. */
  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
    border: 0;
  }

  .status {
    display: inline-block;
    align-self: flex-start;
    margin: 0 0 0.75rem;
    padding: 0.25rem 0.6rem;
    background: rgba(0, 0, 0, 0.75);
    border-radius: 0.4rem;
  }

  /* The tone travels with the sentence rather than being read back out of it, so
     the two tones that ask the user for something are the two the setup window
     uses as well: a line in amber is a line to act on and a line in red is a line
     that says what went wrong. A line with neither is a step of a run. */
  .status[data-tone='warning'] {
    color: #ffc46b;
  }

  .status[data-tone='error'] {
    color: #ff9d9d;
  }

  .placeholder {
    display: inline-block;
    margin: 0.5rem 0 0;
    padding: 0.25rem 0.6rem;
    background: rgba(0, 0, 0, 0.75);
    border-radius: 0.4rem;
    font-size: 0.85rem;
  }

  .content {
    display: grid;
    grid-template-columns: 1fr;
    gap: 1rem;
  }

  /* The screenshot is the one thing above the two text fields and the width of
     all three is the width of the window, so the grid is a single column and the
     fields take the row under it as a pair rather than beside it. */
  .fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
  }

  /* Each field is covered for exactly the phase running inside it and nothing
     else says that phase is under way, so the cover is scoped to one field: a
     layer inside a field asks that field for no track of its own, so a cover
     comes and goes without moving anything, and takes that field's copy button
     with it when it goes. */
  .fields section {
    position: relative;
  }

  /* Each text block gets its own plate of the same thin wash, so the desktop
     reads through the body of the window rather than only around its edges. The
     screenshot shares that one plate rather than carrying a second set of
     numbers, which is what keeps the shot and the two fields reading as one
     surface. The textareas inside stay as dark as they were, which is what keeps
     white text legible over a busy background. */
  .fields section,
  .shot {
    padding: 0.5rem;
    background: rgba(255, 255, 255, 0.04);
    border-radius: 0.5rem;
  }

  /* The wash is the darkest one in the window and the ring sits in the middle of
     it, so the text still under the layer is plainly not what is being read.
     `role="status"` on the container is what has the label read out, and the
     ring is decoration as far as anything listening is concerned. */
  .field-loading {
    position: absolute;
    inset: 0;
    z-index: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.6rem;
    background: rgba(0, 0, 0, 0.72);
    border-radius: 0.5rem;
    font-size: 0.85rem;
  }

  /* A ring drawn with two borders and turned by one keyframe, so the spin costs
     a transform and paints on its own layer. */
  .field-spinner {
    box-sizing: border-box;
    width: 1.5rem;
    height: 1.5rem;
    border: 2px solid rgba(255, 255, 255, 0.3);
    border-top-color: #fff;
    border-radius: 50%;
    animation: goat-field-spin 0.8s linear infinite;
  }

  @keyframes goat-field-spin {
    to {
      transform: rotate(360deg);
    }
  }

  /* Motion asked to be off is motion not asked for: the ring is then a ring and
     the label beside it still says the phase is running. */
  @media (prefers-reduced-motion: reduce) {
    .field-spinner {
      animation: none;
      border-top-color: rgba(255, 255, 255, 0.3);
    }
  }

  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    margin-bottom: 0.4rem;
  }

  .shot canvas {
    width: 100%;
    height: auto;
    display: block;
    border: 1px solid rgba(255, 255, 255, 0.3);
    background: rgba(0, 0, 0, 0.3);
  }

  .shotwrap {
    position: relative;
    display: inline-block;
    max-width: 100%;
  }

  .shotwrap.armed {
    cursor: crosshair;
  }

  .shotwrap.armed canvas {
    pointer-events: none;
  }

  .overlay {
    position: fixed;
    inset: 0;
    cursor: crosshair;
    background: rgba(0, 0, 0, 0.15);
    z-index: 10;
  }

  .overlay-hint {
    position: fixed;
    top: 1rem;
    left: 50%;
    transform: translateX(-50%);
    margin: 0;
    padding: 0.4rem 0.8rem;
    background: rgba(0, 0, 0, 0.75);
    border-radius: 0.4rem;
    pointer-events: none;
  }

  .selrect.screen {
    position: fixed;
  }

  .selrect {
    position: absolute;
    border: 2px dashed #fff;
    background: rgba(255, 255, 255, 0.08);
    pointer-events: none;
  }

  section textarea {
    width: 100%;
    box-sizing: border-box;
    min-height: 5rem;
    padding: 0.5rem;
    background: rgba(0, 0, 0, 0.75);
    border: 1px solid rgba(255, 255, 255, 0.25);
    border-radius: 0.4rem;
    color: #fff;
    font: inherit;
    white-space: pre-wrap;
    word-break: break-word;
    resize: vertical;
  }

  .icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.75rem;
    height: 1.75rem;
    padding: 0;
    background: rgba(255, 255, 255, 0.08);
    color: #fff;
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 0.35rem;
    cursor: pointer;
  }

  .icon:hover:not(:disabled) {
    background: rgba(255, 255, 255, 0.18);
  }

  .icon:disabled {
    opacity: 0.4;
    cursor: default;
  }

  /* The close control sits where the title bar's own button would, so it is a
     bare glyph and the row it lives in is the only thing that gives it a shape
     on hover. */
  .icon.ghost {
    background: transparent;
    border: none;
  }

  .icon.ghost:hover {
    background: rgba(255, 255, 255, 0.1);
  }

  .icon.ghost:focus-visible {
    outline: 2px solid #9ec5ff;
    outline-offset: 2px;
  }
</style>
