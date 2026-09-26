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
mod request;
pub mod status;
mod system;

pub use parse::parse_shortcut;
#[cfg(target_os = "linux")]
pub use status::Backend;
pub use status::HotkeyStatus;

use anyhow::{Result, anyhow};
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

/// The one place a hotkey turns into work. The window-system backend resolves a
/// key press to an `Action` and the portal backend resolves a shortcut id, then
/// both come here.
pub fn dispatch(app: &AppHandle, action: Action) {
    match action {
        Action::ScreenSelect => crate::enter_screen_select(app),
        Action::Capture => {
            crate::show_main_window(app);
            let handle = app.clone();
            tauri::async_runtime::spawn(async move {
                let state = handle.state::<AppState>();
                if let Err(e) = crate::run_pipeline(&handle, &state).await {
                    report(&handle, &format!("Screen capture failed: {e}"));
                }
            });
        }
    }
}

pub fn setup(app: &AppHandle) -> tauri::Result<()> {
    let (capture, select) = seed_state(app);
    start(app, &capture, &select)
}

/// Rejects an unparsable request before it can reach the window system or open
/// a portal dialog, so a typo is reported the same way on every backend.
pub async fn remap(app: &AppHandle, action: Action, previous: &str, next: &str) -> Result<()> {
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
/// problem does not change which backend is in use.
pub fn warn(app: &AppHandle, message: &str) {
    let line = format!("Hotkey problem: {message}");
    let mut status = lock(&app.state::<AppState>().hotkey_status).clone();
    status.warning = line.clone();
    publish(app, &status);
    report(app, &line);
}

#[tauri::command]
pub fn hotkey_status(state: tauri::State<'_, AppState>) -> HotkeyStatus {
    lock(&state.hotkey_status).clone()
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
/// still leave the other one without a trigger.
#[cfg(target_os = "linux")]
fn record_triggers(app: &AppHandle, bound: &dbus::ShortcutList) {
    publish(app, &binding::portal_status(bound));
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
    let (detail, warning) = match portal::start(app).await {
        Ok((_, report)) => (report.detail, report.warning),
        Err(e) => (
            String::new(),
            format!("Global hotkeys are unavailable: {e:#}"),
        ),
    };
    publish(
        app,
        &HotkeyStatus {
            backend: Backend::Portal,
            detail: format!("{} {detail}", status::default_detail(Backend::Portal))
                .trim_end()
                .to_string(),
            warning: warning.clone(),
        },
    );
    notice::startup_notice(app, PORTAL_NOTICE, &warning);
}
