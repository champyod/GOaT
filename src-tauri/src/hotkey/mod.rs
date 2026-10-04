#[cfg(target_os = "linux")]
mod binding;
#[cfg(target_os = "linux")]
mod dbus;
#[cfg(target_os = "linux")]
mod notice;
mod parse;
#[cfg(target_os = "linux")]
mod portal;
#[cfg(target_os = "linux")]
mod reply;
#[cfg(target_os = "linux")]
mod request;
#[cfg(target_os = "linux")]
mod signal;
pub mod status;
mod system;
#[cfg(target_os = "linux")]
mod waiting;

pub use parse::parse_shortcut;
#[cfg(target_os = "linux")]
pub use status::Backend;
pub use status::HotkeyStatus;

use anyhow::{Result, anyhow};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, MutexGuard};
use tauri::{AppHandle, Emitter, Manager};

use crate::AppState;

#[cfg(target_os = "linux")]
const PORTAL_NOTICE: &str = "Your desktop will ask you to approve GOaT and then to press the keys for each GOaT \
     shortcut. The saved hotkey is not filled in for you, so the hotkeys do nothing until a \
     trigger has been chosen.";

/// What a bound trigger does. Each backend turns its own trigger into one of
/// these and hands it to `dispatch`, so the behaviour is written once.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Action {
    Capture,
    ScreenSelect,
}

impl Action {
    pub fn example(self) -> &'static str {
        match self {
            Action::Capture => crate::DEFAULT_HOTKEY,
            Action::ScreenSelect => crate::DEFAULT_SELECT_HOTKEY,
        }
    }
}

/// Set while a triggered capture is in flight. A trigger held down is a key
/// press per repeat, and a second run started on top of the first would grab the
/// screen again mid-read and leave the window holding whichever result arrived
/// last. The button path has its own guard in the window, which is why this is
/// only about the trigger: nothing else starts a capture behind a window's back.
static CAPTURE_IN_PROGRESS: AtomicBool = AtomicBool::new(false);

/// Claims the run, or reports that one is already under way. The press is
/// reported rather than dropped, because a trigger that does nothing while the
/// window is busy reading is indistinguishable from one that is not bound.
fn claim_capture_run(app: &AppHandle) -> bool {
    if CAPTURE_IN_PROGRESS.swap(true, Ordering::SeqCst) {
        report(app, "A capture is already running.");
        return false;
    }
    true
}

/// The one place a hotkey turns into work. The window-system backend resolves a
/// key press to an `Action` and the portal backend resolves a shortcut id, then
/// both come here.
pub fn dispatch(app: &AppHandle, action: Action) {
    match action {
        Action::ScreenSelect => crate::enter_screen_select(app),
        Action::Capture => {
            if !claim_capture_run(app) {
                return;
            }
            let handle = app.clone();
            tauri::async_runtime::spawn(async move { capture_off_screen(&handle).await });
        }
    }
}

/// A capture in the order the window has to see it in: the window goes off the
/// screen for the grab and comes back the moment the pixels are in hand, so the
/// screenshot that follows lands in a window that is already there and the read
/// cannot leave the desktop without the window the key was pressed for.
///
/// The claim is released on the way out of every branch, so a run that failed
/// does not leave the trigger refusing to work for the rest of the session.
async fn capture_off_screen(app: &AppHandle) {
    let state = app.state::<AppState>();
    // A hotkey capture is a whole capture, so it is measured as one: the row is
    // opened here and closed by whichever pipeline reads the pixels, and the
    // grab it holds is the same grab a button press would have paid.
    let mut timing = crate::CaptureTiming::begin();
    let image = match crate::grab_monitor(app, &state, &mut timing).await {
        Ok(image) => image,
        Err(e) => {
            let message = format!("Screen capture failed: {e}");
            report(app, &message);
            crate::show_main_window(app);
            // The window is back and has nothing to show but the reason, so the
            // failure is published rather than left in the log: a first run that
            // never captured would otherwise sit on "Ready" with a grab that
            // cannot be retried until the session ends.
            crate::record_capture_attempted();
            crate::publish_progress(app, &crate::CaptureProgress::failure(&message));
            CAPTURE_IN_PROGRESS.store(false, Ordering::SeqCst);
            return;
        }
    };
    // The grab put the window back as soon as it had the pixels, before the
    // pipeline published the screenshot, so the window is already on its bar by
    // the time this returns the image to the caller.
    if let Err(e) = crate::run_pipeline_with_image(app, &state, image, &mut timing).await {
        let message = format!("Reading the screenshot failed: {e}");
        report(app, &message);
        // The screenshot is already on screen by this point, so the read is the
        // one step a failure can leave without a terminal message to end it.
        crate::publish_progress(app, &crate::CaptureProgress::failure(&message));
    }
    CAPTURE_IN_PROGRESS.store(false, Ordering::SeqCst);
}

pub fn setup(app: &AppHandle) -> tauri::Result<()> {
    let (capture, select) = seed_state(app);
    start(app, &capture, &select)
}

/// Rejects an unparsable request before it can reach the window system or open
/// a portal dialog, so a typo is reported the same way on every backend.
pub async fn remap(app: &AppHandle, action: Action, previous: &str, next: &str) -> Result<()> {
    // A portal session is reconfigured by the desktop's own dialog, so a typed
    // shortcut is not what gets bound and there is nothing to check it against.
    if !binds_typed_trigger(app) {
        return apply(app, action, previous, next).await;
    }
    if parse_shortcut(next).is_none() {
        return Err(anyhow!(
            "invalid shortcut \"{next}\", use e.g. {}",
            action.example()
        ));
    }
    apply(app, action, previous, next).await
}

pub fn report(app: &AppHandle, message: &str) {
    if let Err(e) = app.emit("hotkey-error", message) {
        eprintln!("GOaT: {message} (the window is not reachable: {e})");
    }
}

/// Records a hotkey problem on the persistent status line and pushes it to the
/// window, which may still be hidden. The rest of the line is kept, because the
/// problem does not change which backend is in use. Only the status line carries
/// a warning: the error line is reserved for a command that failed, so a warning
/// is not painted twice and the next reconfigure withdraws it when it no longer
/// holds.
pub fn warn(app: &AppHandle, message: &str) {
    let line = format!("Hotkey problem: {message}");
    let state = app.state::<AppState>();
    let mut recorded = lock(&state.hotkey_status).clone();
    recorded.warning = line;
    publish(app, &with_stored_triggers(&state, recorded));
}

/// Whether the backend that is in use will fire a trigger the user typed. The
/// window system registers the string it is given, so it can be stored; a portal
/// session holds triggers the desktop asked for and ignores anything typed, so
/// storing one would leave the saved field reporting a shortcut nothing is bound
/// to.
#[cfg(target_os = "linux")]
pub fn binds_typed_trigger(app: &AppHandle) -> bool {
    !matches!(backend(app), Backend::Portal)
}

#[cfg(not(target_os = "linux"))]
pub fn binds_typed_trigger(_app: &AppHandle) -> bool {
    true
}

#[tauri::command]
pub fn hotkey_status(state: tauri::State<'_, AppState>) -> HotkeyStatus {
    let recorded = lock(&state.hotkey_status).clone();
    with_stored_triggers(&state, recorded)
}

/// The guidance for a desktop that has no shortcut dialog, for a window that has
/// to say so before the user presses anything. A disabled Choose button cannot
/// produce the message a refused bind would have produced, so the line a bind
/// failure leads with is asked for on its own. It is asked for by a session that
/// runs the portal and nowhere else, so no other platform carries the command.
#[cfg(target_os = "linux")]
#[tauri::command]
pub fn configure_guidance_text() -> String {
    portal::guidance_text()
}

/// A window-system session registers the shortcut it is given, so the managed
/// values are the bound ones and outrank whatever the recorded line was built
/// with — it is written before the config has been read and would otherwise
/// report the defaults as bound. A portal session keeps the triggers its dialog
/// produced, which no stored value can stand in for, so its line is handed on as
/// it stands.
fn with_stored_triggers(state: &AppState, recorded: HotkeyStatus) -> HotkeyStatus {
    if !recorded.binds_stored_trigger() {
        return recorded;
    }
    HotkeyStatus {
        capture_trigger: Some(lock(&state.hotkey).clone()),
        select_trigger: Some(lock(&state.select_hotkey).clone()),
        ..recorded
    }
}

/// A poisoned hotkey mutex means a handler thread panicked while holding it.
/// The stored values are still valid, so the lock is recovered rather than
/// failing every later key press.
pub(crate) fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|e| e.into_inner())
}

#[cfg(target_os = "linux")]
fn backend(app: &AppHandle) -> Backend {
    lock(&app.state::<AppState>().hotkey_status).backend
}

fn publish(app: &AppHandle, status: &HotkeyStatus) {
    *lock(&app.state::<AppState>().hotkey_status) = status.clone();
    if let Err(e) = app.emit("hotkey-status", status) {
        eprintln!("GOaT: the hotkey status line could not be updated ({e})");
    }
}

/// Records the triggers the portal now holds. The whole status line is read out
/// of that list, because a reconfigure can bind the shortcut the user edited and
/// still leave the other one without a trigger. Whether this session's portal has
/// a dialog to open is read off the session itself, so the answer is the same one
/// a remap would be given — and a line rebuilt before the session exists carries
/// no answer at all rather than one nobody has given.
#[cfg(target_os = "linux")]
fn record_triggers(app: &AppHandle, bound: &dbus::ShortcutList) {
    let configure = app
        .try_state::<portal::Portal>()
        .map(|portal| portal.supports_configure());
    publish(app, &binding::portal_status(bound, configure));
}

fn seed_state(app: &AppHandle) -> (String, String) {
    let saved = crate::load_config(app);
    let capture = usable_or(&saved.hotkey, crate::DEFAULT_HOTKEY);
    let select = usable_or(&saved.select_hotkey, crate::DEFAULT_SELECT_HOTKEY);
    let state = app.state::<AppState>();
    *lock(&state.hotkey) = capture.clone();
    *lock(&state.select_hotkey) = select.clone();
    *lock(&state.monitor) = saved.monitor;
    (capture, select)
}

fn usable_or(candidate: &str, fallback: &str) -> String {
    if parse_shortcut(candidate).is_some() {
        candidate.to_string()
    } else {
        fallback.to_string()
    }
}

#[cfg(target_os = "linux")]
fn start(app: &AppHandle, capture: &str, select: &str) -> tauri::Result<()> {
    match backend(app) {
        Backend::System => system::setup(app, capture, select),
        Backend::Portal => {
            let handle = app.clone();
            tauri::async_runtime::spawn(async move { start_portal(&handle).await });
            Ok(())
        }
    }
}

/// Only a Wayland compositor needs the portal, so on every other platform the
/// window system is the one and only backend and the choice needs no runtime
/// test.
#[cfg(not(target_os = "linux"))]
fn start(app: &AppHandle, capture: &str, select: &str) -> tauri::Result<()> {
    system::setup(app, capture, select)
}

#[cfg(target_os = "linux")]
async fn apply(app: &AppHandle, action: Action, previous: &str, next: &str) -> Result<()> {
    match backend(app) {
        Backend::System => system::remap(app, previous, next),
        Backend::Portal => match portal::remap(app, action).await {
            Ok(bound) => {
                record_triggers(app, &bound);
                Ok(())
            }
            Err(e) => Err(anyhow!("{e:#}")),
        },
    }
}

#[cfg(not(target_os = "linux"))]
async fn apply(app: &AppHandle, _action: Action, previous: &str, next: &str) -> Result<()> {
    system::remap(app, previous, next)
}

#[cfg(target_os = "linux")]
async fn start_portal(app: &AppHandle) {
    let report = match portal::start(app).await {
        Ok((_, report)) => report,
        Err(e) => portal::BindingReport {
            detail: String::new(),
            warning: format!("Global hotkeys are unavailable: {e:#}"),
            capture_trigger: None,
            select_trigger: None,
            configure_supported: None,
        },
    };
    let line = HotkeyStatus {
        backend: Backend::Portal,
        detail: format!(
            "{} {}",
            status::default_detail(Backend::Portal),
            report.detail
        )
        .trim_end()
        .to_string(),
        warning: report.warning,
        capture_trigger: report.capture_trigger,
        select_trigger: report.select_trigger,
        // A session that never started has no portal to have answered, so the
        // window is told nothing was answered rather than told no: the warning
        // above already names the failure, and a bind will refuse on its own.
        configure_supported: report.configure_supported,
    };
    publish(app, &line);
    notice::startup_notice(app, PORTAL_NOTICE, &line.warning);
}
