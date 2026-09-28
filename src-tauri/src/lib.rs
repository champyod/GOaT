// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
mod appearance;
mod capture;
mod hotkey;
mod models;

use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(test)]
use hotkey::parse_shortcut;

pub(crate) const DEFAULT_HOTKEY: &str = "Ctrl+Shift+S";
pub(crate) const DEFAULT_SELECT_HOTKEY: &str = "Ctrl+Shift+E";

/// How many problems are kept. The log exists to explain a session that has
/// already gone wrong, and a cap keeps a failure that repeats on every key press
/// from filling memory for the life of the process.
const ERROR_LOG_CAP: usize = 100;

/// The engine names a payload carries. No window reads them today, but the empty
/// string is the one value that means "no engine ran", so they travel on the
/// payload as the wire contract rather than as labels that may be reworded.
const OCR_ENGINE_PRIMARY: &str = "tract";
const OCR_ENGINE_FALLBACK: &str = "tesseract";
const OCR_ENGINE_NONE: &str = "";

/// Set the moment the first screenshot is published. A window the app raises on
/// its own is answered from this rather than from what has happened so far,
/// because a capture is the one fact that ends a launch: before it the app is
/// still waiting to be asked for something, and after it the windows are there
/// for the user to work in.
static CAPTURED: AtomicBool = AtomicBool::new(false);

/// Whether a window the app raises for itself is allowed on screen. It is not
/// until something has been captured, so a launch that never reaches a capture
/// leaves the desktop as it was and the problems recorded before the first one
/// are read when the user goes looking. Only the app's own raising is gated: the
/// tray, a trigger and a command are the user asking, and they open a window
/// whatever has been captured.
fn may_auto_show(captured: &AtomicBool) -> bool {
    captured.load(Ordering::SeqCst)
}

/// The same answer as `may_auto_show`, read from the one latch the app sets and
/// named for the path that asks it about resizing. The window a launch puts on
/// the screen is the bar, and a fitted size belongs to the expanded view a
/// capture is what fills, so the app does not move its own window before then.
pub(crate) fn may_auto_fit() -> bool {
    may_auto_show(&CAPTURED)
}

/// The latch is one way. A capture that has happened is a fact about the session
/// and nothing in the app can take it back, so there is no second answer to give
/// once it has been set.
fn mark_captured(captured: &AtomicBool) {
    captured.store(true, Ordering::SeqCst);
}

/// The latch as the app sets it. A capture that reaches the screen counts even
/// when it failed on the way, because the user asked for a capture and the app
/// has answered them — leaving the latch unset would keep a first-run failure in
/// the log with nothing on screen to point at it.
pub(crate) fn record_capture_attempted() {
    mark_captured(&CAPTURED);
}

/// How long the window is given to leave the screen before a grab is taken. The
/// compositor draws a frame at a time and answering a request to unmap a
/// surface is not the frame that unmap lands in, so a grab taken straight after
/// the answer still finds the window in it. 250ms covers the one or two frames a
/// Wayland compositor needs to be rid of the surface, which is the whole
/// difference between a screenshot of the region the user pointed at and a
/// screenshot of GOaT.
const GRAB_SETTLE: std::time::Duration = std::time::Duration::from_millis(250);

/// Takes the window off the screen and waits for it to actually be gone, so every
/// grab in the app is taken the same way and none of them can photograph this
/// app instead of the desktop behind it. The window stays on top of everything,
/// so a grab taken while it is up is a screenshot of GOaT.
///
/// A refusal is reported rather than swallowed, because the grab that follows is
/// then of this window and the user has no other way to know why.
pub(crate) async fn clear_the_screen(app: &tauri::AppHandle) {
    use tauri::Manager;
    if let Some(window) = app.get_webview_window("main")
        && let Err(e) = window.hide()
    {
        report_error(
            app,
            "capture",
            &format!("The window could not be hidden: {e}"),
        );
    }
    tokio::time::sleep(GRAB_SETTLE).await;
}

fn default_select_hotkey() -> String {
    DEFAULT_SELECT_HOTKEY.to_string()
}

/// How far from the top of the screen the bar is put when the user has not said
/// otherwise, and the range a number is held inside. A negative distance is off
/// the top of the screen and a very large one is under whatever the desktop has
/// down there, and a file the user edited by hand is the only way either arrives,
/// so a value outside the range is brought back rather than obeyed.
const DEFAULT_BAR_TOP_OFFSET: i32 = 28;
const BAR_TOP_OFFSET_MIN: i32 = 0;
const BAR_TOP_OFFSET_MAX: i32 = 500;

fn default_bar_top_offset() -> i32 {
    DEFAULT_BAR_TOP_OFFSET
}

pub(crate) struct AppState {
    pub(crate) ocr: Mutex<Option<pure_onnx_ocr_sync::OcrEngine>>,
    pub(crate) hotkey: Mutex<String>,
    pub(crate) select_hotkey: Mutex<String>,
    pub(crate) monitor: Mutex<usize>,
    pub(crate) full_image: Mutex<Option<capture::CapturedImage>>,
    pub(crate) hotkey_status: Mutex<hotkey::HotkeyStatus>,
    pub(crate) errors: Mutex<Vec<ErrorEntry>>,
}

/// A config file written by an older build carries keys this struct no longer
/// has, and serde ignores a field it does not know rather than refusing the
/// whole file, so removing one is not a reason to reset a user's shortcuts.
#[derive(Clone, serde::Serialize, serde::Deserialize)]
struct UserConfig {
    hotkey: String,
    #[serde(default = "default_select_hotkey")]
    select_hotkey: String,
    monitor: usize,
    /// Whether the user has asked not to be shown the setup window again. It is
    /// read only when a run starts and only written by the setup window itself, so
    /// it lives in the file rather than in managed state.
    #[serde(default)]
    hide_bind_notice: bool,
    #[serde(default)]
    appearance: appearance::AppearanceConfig,
    /// How far from the top of the screen the bar is put, in the logical pixels
    /// a position is asked for in. A file written before this setting existed has
    /// no such key, and a missing one has to mean the distance the bar has always
    /// sat at rather than the top edge of the screen.
    #[serde(default = "default_bar_top_offset")]
    bar_top_offset: i32,
}

impl Default for UserConfig {
    fn default() -> Self {
        Self {
            hotkey: DEFAULT_HOTKEY.to_string(),
            select_hotkey: default_select_hotkey(),
            monitor: 0,
            hide_bind_notice: false,
            appearance: appearance::AppearanceConfig::default(),
            bar_top_offset: default_bar_top_offset(),
        }
    }
}

fn config_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    use tauri::Manager;
    let dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    Ok(dir.join("config.json"))
}

pub(crate) fn load_config(app: &tauri::AppHandle) -> UserConfig {
    config_path(app)
        .and_then(|p| std::fs::read_to_string(p).map_err(|e| e.to_string()))
        .and_then(|s| serde_json::from_str(&s).map_err(|e| e.to_string()))
        .unwrap_or_default()
}

pub(crate) fn save_config(app: &tauri::AppHandle, cfg: &UserConfig) -> Result<(), String> {
    let path = config_path(app)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    let s = serde_json::to_string(cfg).map_err(|e| e.to_string())?;
    std::fs::write(path, s).map_err(|e| e.to_string())
}

#[derive(Clone, serde::Serialize)]
struct ResultPayload {
    image: capture::CapturedImage,
    ocr_text: String,
    translated_text: String,
    ocr_engine: String,
    error: String,
}

/// The name a window subscribes to for the whole of a capture. Every step
/// travels on it, so a window opens one feed and this is the only spelling of it.
pub(crate) const CAPTURE_PROGRESS: &str = "capture-progress";

/// The one message a capture is made of. A window reduces it rather than
/// listening for one event per step, so a step nothing is listening for cannot
/// be the one a run stops on, and a failure carries the reason it failed instead
/// of leaving the window to invent a sentence of its own.
///
/// The fields a step does not have are left off the wire rather than sent empty,
/// because "no image" and "an empty image" are different answers to a question
/// only the phase can ask.
#[derive(serde::Serialize)]
pub(crate) struct CaptureProgress<'a> {
    phase: CapturePhase,
    #[serde(skip_serializing_if = "Option::is_none")]
    image: Option<&'a capture::CapturedImage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    ocr_text: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    payload: Option<&'a ResultPayload>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<&'a str>,
}

/// The steps a capture is made of, in the order they happen. The name is the
/// whole message — a window switches on it — so there is no pair of flags here
/// that can disagree with itself and no spelling of a step the wire can produce
/// and the window does not know.
#[derive(Clone, Copy, PartialEq, Eq, Debug, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum CapturePhase {
    /// The screenshot is up and the read is under way.
    Reading,
    /// The text has landed and the translation is under way.
    Translating,
    /// The run is finished, with the result on it.
    Done,
    /// The run failed, with the reason it failed.
    Error,
}

impl<'a> CaptureProgress<'a> {
    fn bare(phase: CapturePhase) -> Self {
        Self {
            phase,
            image: None,
            ocr_text: None,
            payload: None,
            error: None,
        }
    }

    fn reading(image: &'a capture::CapturedImage) -> Self {
        Self {
            image: Some(image),
            ..Self::bare(CapturePhase::Reading)
        }
    }

    fn translating(ocr_text: &'a str) -> Self {
        Self {
            ocr_text: Some(ocr_text),
            ..Self::bare(CapturePhase::Translating)
        }
    }

    fn done(payload: &'a ResultPayload) -> Self {
        Self {
            payload: Some(payload),
            ..Self::bare(CapturePhase::Done)
        }
    }

    /// A run that failed, with the reason. The reason is the point of the event:
    /// a window told only that a capture failed can say nothing but that.
    pub(crate) fn failure(reason: &'a str) -> Self {
        Self {
            error: Some(reason),
            ..Self::bare(CapturePhase::Error)
        }
    }
}

/// The one place a capture step reaches a window. A window that is not listening
/// is nothing the run can do anything about, so a refused publish is written to
/// the terminal rather than raised into the pipeline it would abandon.
pub(crate) fn publish_progress(app: &tauri::AppHandle, progress: &CaptureProgress<'_>) {
    use tauri::Emitter;
    if let Err(e) = app.emit(CAPTURE_PROGRESS, progress) {
        eprintln!("GOaT: the capture progress could not be published ({e})");
    }
}

/// One line of the diagnostics log. `source` names the part of the app that
/// failed, so a reader can tell a capture problem from a trigger problem without
/// reading the message.
#[derive(Clone, serde::Serialize)]
struct ErrorEntry {
    time: String,
    source: String,
    message: String,
}

/// The one place a problem is written down. Every window reads its errors from
/// here rather than painting them into itself, so a failure the user did not
/// cause on purpose is still on screen when they go looking for it.
fn report_error(app: &tauri::AppHandle, source: &str, message: &str) {
    use tauri::{Emitter, Manager};
    let entry = ErrorEntry {
        time: timestamp_now(),
        source: source.to_string(),
        message: message.to_string(),
    };
    {
        let state = app.state::<AppState>();
        let mut log = hotkey::lock(&state.errors);
        push_error(&mut log, entry.clone());
    }
    if let Err(e) = app.emit("app-error", &entry) {
        eprintln!("GOaT: the error log could not be published ({e})");
    }
    #[cfg(desktop)]
    show_error_window(app);
}

/// Keeps the log inside its cap and stops one failure being written twice: a
/// command that fails is recorded by the backend and forwarded again by the
/// window that awaited it, and the same reason arriving twice is one problem.
fn push_error(log: &mut Vec<ErrorEntry>, entry: ErrorEntry) {
    if log.last().is_some_and(|last| last.message == entry.message) {
        return;
    }
    log.push(entry);
    if log.len() > ERROR_LOG_CAP {
        let overflow = log.len() - ERROR_LOG_CAP;
        log.drain(..overflow);
    }
}

/// `YYYY-MM-DDTHH:MM:SSZ`. No date library is a dependency, and a diagnostics
/// line only has to be readable and ordered, so the civil date is derived from
/// the day count with Hinnant's algorithm.
fn timestamp_now() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or_default();
    let (year, month, day) = civil_from_days(seconds.div_euclid(86_400));
    let time = seconds.rem_euclid(86_400);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        time / 3_600,
        (time % 3_600) / 60,
        time % 60
    )
}

fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = (shifted - era * 146_097) as u32;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era as i64 + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

#[tauri::command]
fn list_errors(state: tauri::State<'_, AppState>) -> Vec<ErrorEntry> {
    hotkey::lock(&state.errors).clone()
}

#[tauri::command]
fn clear_errors(state: tauri::State<'_, AppState>) -> usize {
    let mut log = hotkey::lock(&state.errors);
    let cleared = log.len();
    log.clear();
    cleared
}

/// A window that catches a failed command has no way to reach the log itself, so
/// it hands the reason over here and stays silent.
#[tauri::command]
fn report_frontend_error(app: tauri::AppHandle, source: String, message: String) {
    report_error(&app, &source, &message);
}

/// The window that records a problem opens itself when one arrives, and an
/// already open one is left exactly as it is: a capture that fails twice must
/// not pull the window away from the screenshot the user is working on. Nothing
/// opens before the first capture either, because a problem the user has not
/// asked anything for yet is a log entry waiting to be read rather than a window
/// to put in front of a desktop they are working in.
#[cfg(desktop)]
fn show_error_window(app: &tauri::AppHandle) {
    use tauri::Manager;
    if !may_auto_show(&CAPTURED) {
        return;
    }
    let Some(window) = app.get_webview_window("errors") else {
        return;
    };
    if window.is_visible().unwrap_or(false) {
        return;
    }
    let _ = window.show();
    let _ = window.set_focus();
}

/// Every hotkey problem already reaches the app as `hotkey-error`, so the log is
/// fed from that one event instead of from a second reporting path inside the
/// hotkey module. This is registered before the backends start, so a failure
/// during startup is recorded like any other.
#[cfg(desktop)]
fn forward_hotkey_errors(app: &tauri::AppHandle) {
    use tauri::Listener;
    let handle = app.clone();
    let _listener = app.listen("hotkey-error", move |event| {
        let payload = event.payload();
        let message =
            serde_json::from_str::<String>(payload).unwrap_or_else(|_| payload.to_string());
        report_error(&handle, "hotkey", &message);
    });
}

#[tauri::command]
fn init_models(app: tauri::AppHandle, state: tauri::State<'_, AppState>) -> Result<String, String> {
    let mut guard = state.ocr.lock().map_err(|e| e.to_string())?;
    if guard.is_none() {
        let engine = models::load_ocr_engine(&app).map_err(|e| format!("{e}"))?;
        *guard = Some(engine);
    }
    Ok("OCR models loaded".to_string())
}

#[tauri::command]
fn models_status(app: tauri::AppHandle) -> models::ModelsStatus {
    models::models_status(&app)
}

fn ensure_engine<'a>(
    app: &tauri::AppHandle,
    guard: &'a mut std::sync::MutexGuard<'_, Option<pure_onnx_ocr_sync::OcrEngine>>,
) -> Result<&'a pure_onnx_ocr_sync::OcrEngine, String> {
    if guard.is_none() {
        let engine = models::load_ocr_engine(app).map_err(|e| format!("{e}"))?;
        **guard = Some(engine);
    }
    guard
        .as_ref()
        .ok_or_else(|| "OCR engine not loaded".to_string())
}

#[tauri::command]
fn hide_window(app: tauri::AppHandle) -> Result<(), String> {
    use tauri::Manager;
    if let Some(window) = app.get_webview_window("main") {
        window.hide().map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// What the desktop now holds for a login, and the place it keeps it. The
/// window is handed the entry itself as well as the state, because a control
/// that can only show a boolean looks exactly the same whether an entry was
/// written or the call did nothing at all. `is_dev` says whether this build is
/// one that refuses to write an entry at all, so a switch can be off for a
/// reason rather than for a state.
#[derive(Clone, serde::Serialize)]
struct AutostartState {
    enabled: bool,
    path: String,
    is_dev: bool,
}

/// Why a build that is not a release may not touch the login entry, or `None`
/// when it may. The entry is written into the user's own autostart folder and
/// read at every login, so a dev binary started from a checkout would leave
/// behind an entry that launches a path nobody will ever build again — a stale
/// GOaT at every login, with nothing in the app that could explain or remove it.
/// Refusing is the cheap direction to be wrong in: what it costs is the setting
/// in a dev build, which is where the setting is not wanted anyway.
fn autostart_refusal(is_dev: bool) -> Option<String> {
    if !is_dev {
        return None;
    }
    Some("this is a development build, so it will not add itself to your login items".to_string())
}

/// The file `auto-launch` writes on Linux: `~/.config/autostart/{name}.desktop`.
/// Both halves come from the sources the plugin takes them from — the home
/// folder from the path resolver, which asks the same `dirs::home_dir()` the
/// plugin does, and the name from the package info the plugin is handed — so a
/// product name that changes moves this check along with it.
#[cfg(target_os = "linux")]
fn autostart_entry(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    use tauri::Manager;
    let home = app
        .path()
        .home_dir()
        .map_err(|e| format!("the home folder could not be located: {e}"))?;
    Ok(home
        .join(".config")
        .join("autostart")
        .join(format!("{}.desktop", app.package_info().name)))
}

/// A call the plugin reports as done but did not do is the whole reason this
/// command checks, so the entry is read back off the disk before the new state
/// leaves here. The other platforms register the entry under its name rather
/// than keeping it in a file this app can point at, so the plugin's own answer
/// is the best one available there.
#[cfg(target_os = "linux")]
fn verified_autostart(
    app: &tauri::AppHandle,
    _manager: &tauri_plugin_autostart::AutoLaunchManager,
    enabled: bool,
) -> Result<AutostartState, String> {
    let entry = autostart_entry(app)?;
    let path = entry.display().to_string();
    if entry.exists() != enabled {
        return Err(if enabled {
            format!("the login entry was not written to {path}")
        } else {
            format!("the login entry was not removed from {path}")
        });
    }
    Ok(AutostartState {
        enabled,
        path,
        is_dev: tauri::is_dev(),
    })
}

#[cfg(not(target_os = "linux"))]
fn verified_autostart(
    app: &tauri::AppHandle,
    manager: &tauri_plugin_autostart::AutoLaunchManager,
    _enabled: bool,
) -> Result<AutostartState, String> {
    use tauri::Manager;
    let enabled = manager.is_enabled().map_err(|e| e.to_string())?;
    Ok(AutostartState {
        enabled,
        path: app.package_info().name.clone(),
        is_dev: tauri::is_dev(),
    })
}

#[tauri::command]
fn set_autostart(app: tauri::AppHandle, enabled: bool) -> Result<AutostartState, String> {
    if let Some(refused) = autostart_refusal(tauri::is_dev()) {
        return Err(refused);
    }
    use tauri_plugin_autostart::ManagerExt;
    let manager = app.autolaunch();
    if enabled {
        manager.enable().map_err(|e| e.to_string())?;
    } else {
        manager.disable().map_err(|e| e.to_string())?;
    }
    verified_autostart(&app, &manager, enabled)
}

/// The state the desktop holds right now, asked without changing anything, in
/// the same shape a write answers in. Linux answers from the file, because the
/// file is the only place the entry is really kept and its presence is the only
/// proof the desktop will act on one. The other platforms register the entry
/// under its name rather than in a file this app can point at, so there the
/// plugin's own answer stands in for both fields.
#[tauri::command]
fn is_autostart(app: tauri::AppHandle) -> Result<AutostartState, String> {
    current_autostart(&app)
}

#[cfg(target_os = "linux")]
fn current_autostart(app: &tauri::AppHandle) -> Result<AutostartState, String> {
    let entry = autostart_entry(app)?;
    Ok(AutostartState {
        enabled: entry.exists(),
        path: entry.display().to_string(),
        is_dev: tauri::is_dev(),
    })
}

#[cfg(not(target_os = "linux"))]
fn current_autostart(app: &tauri::AppHandle) -> Result<AutostartState, String> {
    use tauri_plugin_autostart::ManagerExt;
    let enabled = app.autolaunch().is_enabled().map_err(|e| e.to_string())?;
    Ok(AutostartState {
        enabled,
        path: app.package_info().name.clone(),
        is_dev: tauri::is_dev(),
    })
}

pub(crate) async fn run_pipeline(
    app: &tauri::AppHandle,
    state: &tauri::State<'_, AppState>,
) -> Result<ResultPayload, String> {
    let image = grab_monitor(app, state).await?;
    run_pipeline_with_image(app, state, image).await
}

/// The pixels of the whole monitor, taken the way every grab in this app is
/// taken: the window off the screen first, and long enough for it to be gone.
/// The stored screenshot is what a selection crops from, so it is written here
/// rather than by the callers that happen to be holding the image.
pub(crate) async fn grab_monitor(
    app: &tauri::AppHandle,
    state: &tauri::State<'_, AppState>,
) -> Result<capture::CapturedImage, String> {
    clear_the_screen(app).await;
    let monitor = *state.monitor.lock().map_err(|e| e.to_string())?;
    let image = capture::capture_monitor(monitor).or_else(|_| capture::capture_primary())?;
    *state.full_image.lock().map_err(|e| e.to_string())? = Some(image.clone());
    restore_screen(app);
    Ok(image)
}

/// Puts the window back once the pixels are in hand. A grab takes it off the
/// screen on every platform, and a window that never comes back is a capture the
/// user has no result to look at. It goes back before the pipeline publishes the
/// screenshot, so the window returns on its bar and the picture arrives into a
/// window that is already there.
#[cfg(desktop)]
fn restore_screen(app: &tauri::AppHandle) {
    show_main_window(app);
}

#[cfg(not(desktop))]
fn restore_screen(_app: &tauri::AppHandle) {}

#[tauri::command]
async fn capture_region(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    monitor: usize,
    x: u32,
    y: u32,
    width: u32,
    height: u32,
) -> Result<ResultPayload, String> {
    // The overlay the user picked in is this app's own window, so it has to be off
    // the screen on the same terms as the whole-window grab: a region read with
    // the window still up is a read of GOaT.
    clear_the_screen(&app).await;
    let image = capture::capture_monitor_region(monitor, x, y, width, height)?;
    *state.full_image.lock().map_err(|e| e.to_string())? = Some(image.clone());
    restore_screen(&app);
    run_pipeline_with_image(&app, &state, image).await
}

async fn run_pipeline_with_image(
    app: &tauri::AppHandle,
    state: &tauri::State<'_, AppState>,
    image: capture::CapturedImage,
) -> Result<ResultPayload, String> {
    // The latch is set before the screenshot is published rather than after, so
    // a problem raised while this first capture is still being read is already
    // allowed to open the window it would have opened after it.
    mark_captured(&CAPTURED);
    // Show the screenshot immediately; OCR/translate follow on the full image.
    // This is the only place a capture hands a window its pixels, and the step
    // they belong to travels with them: a window painting an image it was told
    // nothing about has to guess whether the read is under way.
    publish_progress(app, &CaptureProgress::reading(&image));
    let read = ocr_with_fallback(app, state, &image).await?;
    let ocr_text = read.text;
    let ocr_engine = read.engine;
    let mut error = read.error;
    // The read is finished before the translation starts, so the text is
    // published on that boundary and a window can stop waiting on the read.
    publish_progress(app, &CaptureProgress::translating(&ocr_text));
    let translated_text = match translate_if_any(app, &ocr_text) {
        Ok(text) => text,
        Err(reason) => {
            if error.is_empty() {
                report_error(app, "translate", &reason);
                error = reason;
            }
            String::new()
        }
    };
    let payload = ResultPayload {
        image,
        ocr_text,
        translated_text,
        ocr_engine: ocr_engine.to_string(),
        error,
    };
    publish_progress(app, &CaptureProgress::done(&payload));
    Ok(payload)
}

#[tauri::command]
async fn ocr_selection(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    x: u32,
    y: u32,
    width: u32,
    height: u32,
) -> Result<ResultPayload, String> {
    let image = state
        .full_image
        .lock()
        .map_err(|e| e.to_string())?
        .clone()
        .ok_or_else(|| "no screenshot yet, capture first".to_string())?;
    let crop = image.crop(x, y, width, height)?;
    run_pipeline_with_image(&app, &state, crop).await
}

#[tauri::command]
async fn capture_primary(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
) -> Result<ResultPayload, String> {
    run_pipeline(&app, &state).await
}

/// What one read of a screenshot produced. `error` is empty unless neither
/// engine could read the image, because a payload is published either way and a
/// read that found nothing is a result the windows still have to show.
struct OcrRead {
    text: String,
    engine: &'static str,
    error: String,
}

/// Reads the image with the bundled engine and, when that fails for any reason,
/// with the sidecar. The model lock is taken for the bundled run only and
/// released before the sidecar starts, so a slow fallback cannot block the
/// commands that wait on that lock.
async fn ocr_with_fallback(
    app: &tauri::AppHandle,
    state: &tauri::State<'_, AppState>,
    image: &capture::CapturedImage,
) -> Result<OcrRead, String> {
    let dynamic = image.to_dynamic_image()?;
    let primary = state
        .ocr
        .lock()
        .map_err(|e| e.to_string())
        .and_then(|mut guard| {
            ensure_engine(app, &mut guard)
                .and_then(|engine| models::run_ocr(engine, &dynamic).map_err(|e| format!("{e}")))
        });
    match primary {
        Ok(text) => Ok(OcrRead {
            text,
            engine: OCR_ENGINE_PRIMARY,
            error: String::new(),
        }),
        Err(primary_err) => match models::run_ocr_fallback(app, &dynamic)
            .await
            .map_err(|e| format!("{e}"))
        {
            Ok(text) => Ok(OcrRead {
                text,
                engine: OCR_ENGINE_FALLBACK,
                error: String::new(),
            }),
            Err(fallback_err) => Ok(OcrRead {
                text: String::new(),
                engine: OCR_ENGINE_NONE,
                error: format!("{primary_err}; fallback OCR also failed: {fallback_err}"),
            }),
        },
    }
}

/// A blank read is not sent to the translator: there is nothing to translate,
/// and a translation of nothing is not a result the user could act on.
fn translate_if_any(app: &tauri::AppHandle, ocr_text: &str) -> Result<String, String> {
    if ocr_text.trim().is_empty() {
        return Ok(String::new());
    }
    models::run_translate(app, ocr_text).map_err(|e| format!("{e}"))
}

#[cfg(desktop)]
pub(crate) fn show_main_window(app: &tauri::AppHandle) {
    use tauri::Manager;
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg(desktop)]
fn setup_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    use tauri::menu::{Menu, MenuItem};
    use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};

    let show_i = MenuItem::with_id(app, "show", "Show GOaT", true, None::<&str>)?;
    let quit_i = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_i, &quit_i])?;

    let mut tray = TrayIconBuilder::with_id("main-tray")
        .tooltip("GOaT")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main_window(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

#[cfg(desktop)]
pub(crate) fn enter_screen_select(app: &tauri::AppHandle) {
    use tauri::{Emitter, Manager};
    let (mx, my) = {
        let state = app.state::<AppState>();
        let index = *state.monitor.lock().unwrap_or_else(|e| e.into_inner());
        capture::list_monitors()
            .ok()
            .and_then(|ms| ms.into_iter().find(|m| m.index == index))
            .map(|m| (m.x, m.y))
            .unwrap_or((0, 0))
    };
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        // KWin ignores a position request like this one and the call is a no-op on
        // a Wayland session; the crop offset finishScreenSelect adds covers that.
        let _ = window.set_position(tauri::Position::Physical(tauri::PhysicalPosition {
            x: mx,
            y: my,
        }));
        let _ = window.set_fullscreen(true);
        let _ = window.set_focus();
    }
    let _ = app.emit("region-select", ());
}

/// Whether at least one trigger is left without one. The status line is the one
/// place a backend records that, so it decides whether anything is unbound.
#[cfg(desktop)]
fn triggers_unbound(app: &tauri::AppHandle) -> bool {
    use tauri::Manager;
    !hotkey::lock(&app.state::<AppState>().hotkey_status)
        .warning
        .is_empty()
}

#[cfg(desktop)]
fn show_bind_setup(app: &tauri::AppHandle) {
    use tauri::Manager;
    let Some(window) = app.get_webview_window("setup") else {
        return;
    };
    if window.is_visible().unwrap_or(false) {
        return;
    }
    let _ = window.show();
    let _ = window.set_focus();
}

/// The main window starts hidden, so an unbound trigger leaves nothing on screen
/// to act on and the setup window is shown in its place. The portal backend only
/// finds out after an awaited dialog, so the status line is watched as well as
/// read here: reading it once would open the window for a backend that fails
/// during startup and never for one that fails later. Neither is opened before
/// the first capture, so a launch that never gets that far is not interrupted by
/// a trigger the user has not been asked to choose yet, and the status line is
/// read when they are.
#[cfg(desktop)]
fn watch_unbound_triggers(app: &tauri::AppHandle) {
    use tauri::Listener;
    if load_config(app).hide_bind_notice {
        return;
    }
    let handle = app.clone();
    let _listener = app.listen("hotkey-status", move |_| {
        if may_auto_show(&CAPTURED) && triggers_unbound(&handle) {
            show_bind_setup(&handle);
        }
    });
    if may_auto_show(&CAPTURED) && triggers_unbound(app) {
        show_bind_setup(app);
    }
}

#[tauri::command]
fn get_hotkey(state: tauri::State<'_, AppState>) -> Result<String, String> {
    state
        .hotkey
        .lock()
        .map(|g| g.clone())
        .map_err(|e| e.to_string())
}

/// Binds one trigger, records it and persists the block, so a capture and a
/// region trigger are set by the same steps in the same order.
#[tauri::command]
async fn set_hotkey(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    hotkey: String,
) -> Result<String, String> {
    set_trigger(&app, &state, hotkey::Action::Capture, hotkey).await
}

/// Binds one trigger. A backend that fires only the trigger its own dialog
/// produced keeps the one it already holds, so the field never reports a
/// shortcut that no trigger is bound to.
async fn set_trigger(
    app: &tauri::AppHandle,
    state: &tauri::State<'_, AppState>,
    action: hotkey::Action,
    next: String,
) -> Result<String, String> {
    let cell = trigger_cell(state, action);
    let previous = hotkey::lock(cell).clone();
    remap_trigger(app, action, &previous, &next).await?;
    if !hotkey::binds_typed_trigger(app) {
        return Ok(previous);
    }
    *hotkey::lock(cell) = next.clone();
    persist(app, state)?;
    Ok(next)
}

/// The state cell holding the trigger this action fires on, so a key press and a
/// remap read and write one source for it.
fn trigger_cell(state: &AppState, action: hotkey::Action) -> &std::sync::Mutex<String> {
    match action {
        hotkey::Action::Capture => &state.hotkey,
        hotkey::Action::ScreenSelect => &state.select_hotkey,
    }
}

/// A trigger that cannot be reconfigured is reported to the window that asked
/// for it, which is open on screen and shows the reason on its own status line.
/// Only a failure the user did not cause on purpose reaches the diagnostics log,
/// so binding a shortcut never pulls the errors window up over this one.
async fn remap_trigger(
    app: &tauri::AppHandle,
    action: hotkey::Action,
    previous: &str,
    next: &str,
) -> Result<(), String> {
    hotkey::remap(app, action, previous, next)
        .await
        .map_err(|e| format!("{e:#}"))
}

#[tauri::command]
fn get_select_hotkey(state: tauri::State<'_, AppState>) -> Result<String, String> {
    state
        .select_hotkey
        .lock()
        .map(|g| g.clone())
        .map_err(|e| e.to_string())
}

#[tauri::command]
async fn set_select_hotkey(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    hotkey: String,
) -> Result<String, String> {
    set_trigger(&app, &state, hotkey::Action::ScreenSelect, hotkey).await
}

#[tauri::command]
fn get_monitor(state: tauri::State<'_, AppState>) -> Result<usize, String> {
    state.monitor.lock().map(|g| *g).map_err(|e| e.to_string())
}

#[tauri::command]
fn set_monitor(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    monitor: usize,
) -> Result<usize, String> {
    *hotkey::lock(&state.monitor) = monitor;
    persist(&app, &state)?;
    Ok(monitor)
}

/// The desktop this build is running on, in the name the standard library uses.
/// The frontend has to be told rather than read the user agent: Tauri moved its
/// own `os` module out into a plugin, so the API package offers no way to ask,
/// and a webview naming its own platform is naming what it was sent rather than
/// what it is on. The names are `macos`, `windows` and `linux` — not `darwin`
/// and `win32`, which is the spelling this information used to travel in.
#[tauri::command]
fn os_platform() -> &'static str {
    std::env::consts::OS
}

/// The whole config as it stands: the live hotkey block, plus the notice flag and
/// the appearance as the file holds them. Reading the block from managed state and
/// the rest from disk is what keeps a write of one from putting the other's saved
/// value back.
fn config_from_state(app: &tauri::AppHandle, state: &tauri::State<'_, AppState>) -> UserConfig {
    let stored = load_config(app);
    UserConfig {
        hotkey: hotkey::lock(&state.hotkey).clone(),
        select_hotkey: hotkey::lock(&state.select_hotkey).clone(),
        monitor: *hotkey::lock(&state.monitor),
        hide_bind_notice: stored.hide_bind_notice,
        appearance: stored.appearance,
        bar_top_offset: stored.bar_top_offset,
    }
}

/// Writes the whole hotkey block from live state, so every command that touches
/// one of these values persists all of them.
fn persist(app: &tauri::AppHandle, state: &tauri::State<'_, AppState>) -> Result<(), String> {
    save_config(app, &config_from_state(app, state))
}

#[tauri::command]
fn get_hide_bind_notice(app: tauri::AppHandle) -> bool {
    load_config(&app).hide_bind_notice
}

#[tauri::command]
fn set_hide_bind_notice(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    hide: bool,
) -> Result<bool, String> {
    let mut cfg = config_from_state(&app, &state);
    cfg.hide_bind_notice = hide;
    save_config(&app, &cfg)?;
    Ok(hide)
}

/// The distance the bar is put from the top of the screen, in the logical pixels
/// a position is asked for in. The file is the only place it is kept, so this is
/// read off it rather than out of managed state, the same way the notice flag is.
#[tauri::command]
fn get_bar_top_offset(app: tauri::AppHandle) -> i32 {
    load_config(&app).bar_top_offset
}

/// The distance the bar is put at, held inside a range a bar can be placed in.
/// The file is editable by hand, and a window asked for an offset past the bottom
/// of the screen is one the desktop can only refuse or bury, so a value outside
/// the range is brought back into it rather than obeyed.
fn clamped_bar_top_offset(offset: i32) -> i32 {
    offset.clamp(BAR_TOP_OFFSET_MIN, BAR_TOP_OFFSET_MAX)
}

/// Records the distance the user chose, tells the windows about it, and answers
/// with the value that was kept, so the bar is moved to the number the file
/// actually holds and both windows show that one rather than the one that was
/// sent.
///
/// The broadcast is what makes the slider move the bar while the app is open:
/// the file alone would only take effect on the next launch, because the window
/// is placed once at startup and has no other reason to look.
#[tauri::command]
fn set_bar_top_offset(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    offset: i32,
) -> Result<i32, String> {
    use tauri::Emitter;
    let kept = clamped_bar_top_offset(offset);
    let mut cfg = config_from_state(&app, &state);
    cfg.bar_top_offset = kept;
    save_config(&app, &cfg)?;
    let _ = app.emit("bar-top-offset-changed", &kept);
    Ok(kept)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .manage(AppState {
            ocr: Mutex::new(None),
            hotkey: Mutex::new(DEFAULT_HOTKEY.to_string()),
            select_hotkey: Mutex::new(default_select_hotkey()),
            monitor: Mutex::new(0),
            full_image: Mutex::new(None),
            hotkey_status: Mutex::new(hotkey::status::initial()),
            errors: Mutex::new(Vec::new()),
        });
    // The notice reads the managed plugin state, so the registration cannot be
    // decided at runtime, but only the Wayland backend shows a notice and only a
    // Wayland session can run one. macOS and Windows carry neither the plugin nor
    // this line.
    #[cfg(target_os = "linux")]
    let builder = builder.plugin(tauri_plugin_notification::init());
    builder
        .setup(|app| {
            #[cfg(desktop)]
            {
                use tauri::Manager;
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.hide();
                }
                setup_tray(app.handle())?;
                forward_hotkey_errors(app.handle());
                hotkey::setup(app.handle())?;
                watch_unbound_triggers(app.handle());
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // The main window is the app itself, so closing it only hides it. The
            // setup and errors windows are dialogs and their close buttons have
            // to end them, or they could never be dismissed.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event
                && window.label() == "main"
            {
                let _ = window.hide();
                api.prevent_close();
            }
            // The menu is a window of the app rather than a task of the user's,
            // so losing focus is the end of it. It hides rather than closes,
            // because the bar raises it again on the next press and a closed one
            // would have to be built from scratch to answer. Only a real focus
            // loss counts: the desktop taking the pointer away from the window is
            // not the user putting the menu down.
            if let tauri::WindowEvent::Focused(false) = event
                && window.label() == "menu"
            {
                let _ = window.hide();
            }
        })
        .invoke_handler(tauri::generate_handler![
            init_models,
            models_status,
            capture::list_monitors,
            capture_primary,
            capture_region,
            ocr_selection,
            get_monitor,
            set_monitor,
            os_platform,
            hide_window,
            set_autostart,
            is_autostart,
            get_hotkey,
            set_hotkey,
            get_select_hotkey,
            set_select_hotkey,
            get_hide_bind_notice,
            set_hide_bind_notice,
            get_bar_top_offset,
            set_bar_top_offset,
            appearance::get_appearance,
            appearance::set_appearance,
            appearance::set_window_size,
            hotkey::hotkey_status,
            // The guidance for a desktop with no shortcut dialog belongs to the
            // portal session that has none, and no other platform runs one.
            #[cfg(target_os = "linux")]
            hotkey::configure_guidance_text,
            list_errors,
            clear_errors,
            report_frontend_error
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;
    use tauri_plugin_global_shortcut::{Code, Modifiers};

    fn entry(message: &str) -> ErrorEntry {
        ErrorEntry {
            time: "2026-01-01T00:00:00Z".to_string(),
            source: "test".to_string(),
            message: message.to_string(),
        }
    }

    #[test]
    fn push_error_keeps_the_newest_inside_the_cap() {
        let mut log: Vec<ErrorEntry> = Vec::new();
        for index in 0..(ERROR_LOG_CAP + 5) {
            push_error(&mut log, entry(&format!("failure {index}")));
        }
        assert_eq!(log.len(), ERROR_LOG_CAP);
        assert_eq!(log[0].message, "failure 5");
        assert_eq!(log[ERROR_LOG_CAP - 1].message, "failure 104");
    }

    #[test]
    fn push_error_keeps_one_row_for_a_failure_reported_twice() {
        let mut log = vec![entry("first")];
        push_error(&mut log, entry("first"));
        push_error(&mut log, entry("second"));
        push_error(&mut log, entry("first"));
        assert_eq!(log.len(), 3);
    }

    /// The frontend switches on exactly these three names, and treats anything
    /// else as a desktop it cannot blur. A name outside them would leave the
    /// control permanently disabled on a desktop that could honour it, which is
    /// the same spelling mistake the API package once shipped.
    #[test]
    fn the_platform_is_named_one_the_frontend_understands() {
        assert!(
            matches!(os_platform(), "macos" | "windows" | "linux"),
            "unexpected platform name: {}",
            os_platform()
        );
    }

    /// The one answer a window the app shapes by itself is read from, and the
    /// two things it decides: whether the app may raise a window, and whether it
    /// may resize one. A launch is the bar, so neither happens before a capture
    /// has made the body worth having. What the user asks for is not in here —
    /// the tray, a trigger and a command open a window whatever has been
    /// captured.
    #[test]
    fn a_launch_raises_no_window_of_its_own_before_the_first_capture() {
        let captured = AtomicBool::new(false);
        assert!(
            !may_auto_show(&captured),
            "a launch is silent until something has been captured"
        );
        mark_captured(&captured);
        assert!(
            may_auto_show(&captured),
            "the first capture is what lets the app open a window of its own"
        );
    }

    /// A dev binary that wrote a login entry would leave a stale GOaT at every
    /// login, pointing at a build directory nobody publishes, so the write is
    /// refused before it reaches the plugin. This build is the one the refusal
    /// is defined against, and the test says so rather than assuming it.
    #[test]
    fn a_development_build_never_writes_a_login_entry() {
        let refused = autostart_refusal(tauri::is_dev())
            .expect("this build is not a release, so it must not write an entry");
        assert!(
            refused.contains("development build"),
            "the refusal names what it is refusing for: {refused}"
        );
    }

    /// A window reduces one message for a whole capture, so a step name it does
    /// not know, or a reason that never reaches it, is a run that stops with the
    /// covers still up. These are the names the window switches on, and a step
    /// carries only the field it has — a step with an empty image is not the same
    /// answer as a step with no image.
    #[test]
    fn capture_progress_carries_the_step_it_names_and_nothing_else() {
        let image = capture::CapturedImage {
            width: 1,
            height: 1,
            rgba: vec![0, 0, 0, 0],
        };
        let read = serde_json::to_value(CaptureProgress::reading(&image)).expect("reads");
        assert_eq!(read["phase"], "reading");
        assert_eq!(read["image"]["width"], 1);
        for absent in ["ocr_text", "payload", "error"] {
            assert!(
                read.get(absent).is_none(),
                "a step leaves the fields it does not have off the wire: {read}"
            );
        }

        let translating =
            serde_json::to_value(CaptureProgress::translating("hello")).expect("reads");
        assert_eq!(translating["phase"], "translating");
        assert_eq!(translating["ocr_text"], "hello");

        let payload = ResultPayload {
            image,
            ocr_text: "hello".to_string(),
            translated_text: "bonjour".to_string(),
            ocr_engine: OCR_ENGINE_PRIMARY.to_string(),
            error: String::new(),
        };
        let done = serde_json::to_value(CaptureProgress::done(&payload)).expect("reads");
        assert_eq!(done["phase"], "done");
        assert_eq!(done["payload"]["translated_text"], "bonjour");

        let reason = "Screen capture failed: the display went away";
        let failed = serde_json::to_value(CaptureProgress::failure(reason)).expect("reads");
        assert_eq!(failed["phase"], "error");
        assert_eq!(
            failed["error"], reason,
            "the window is shown the reason rather than a sentence of its own"
        );
    }

    /// The other branch is the one a packaged build lives in, and it is pinned
    /// here rather than left to the build it happens to be compiled under — a
    /// guard that refuses everywhere would cost a real user the setting, and one
    /// that refuses nowhere would cost them a stale login entry.
    #[test]
    fn only_a_development_build_is_refused() {
        assert!(
            autostart_refusal(false).is_none(),
            "a packaged build is the one that owns the login entry"
        );
    }

    #[test]
    fn civil_from_days_matches_known_utc_days() {
        // 0, 1451606400, 951868800 and 951782400 seconds after the epoch are
        // 1970-01-01, 2016-01-01, 2000-03-01 and 2000-02-29.
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(16_801), (2016, 1, 1));
        assert_eq!(civil_from_days(11_017), (2000, 3, 1));
        assert_eq!(civil_from_days(11_016), (2000, 2, 29));
        assert_eq!(civil_from_days(16_860), (2016, 2, 29));
    }

    #[test]
    fn timestamp_is_a_utc_civil_clock() {
        let stamp = timestamp_now();
        assert_eq!(stamp.len(), 20, "unexpected shape: {stamp}");
        assert!(stamp.ends_with('Z'), "unexpected shape: {stamp}");
        assert!(stamp.starts_with("20"), "unexpected shape: {stamp}");
    }

    /// A file written before the setting existed carries no such key, and
    /// loading it must not cost the user the rest of their config over a field
    /// that has a default: the bar is placed where it always has been.
    #[test]
    fn a_config_written_before_the_bar_offset_existed_sits_the_bar_where_it_always_did() {
        let config: UserConfig = serde_json::from_str(
            r#"{"hotkey":"Ctrl+Shift+S","select_hotkey":"Ctrl+Shift+E","monitor":0,"hide_bind_notice":false}"#,
        )
        .expect("an older config still loads");
        assert_eq!(config.bar_top_offset, DEFAULT_BAR_TOP_OFFSET);
    }

    /// A hand-edited file is the only way a number outside the range arrives, and
    /// an offset past the bottom of the screen is a bar the desktop can only
    /// refuse or bury, so it is brought back rather than obeyed.
    #[test]
    fn a_bar_offset_outside_the_range_it_can_be_placed_in_is_brought_back() {
        assert_eq!(clamped_bar_top_offset(-40), BAR_TOP_OFFSET_MIN);
        assert_eq!(clamped_bar_top_offset(9_000), BAR_TOP_OFFSET_MAX);
        assert_eq!(clamped_bar_top_offset(64), 64);
    }

    #[test]
    fn parses_default_hotkey() {
        let s = parse_shortcut(DEFAULT_HOTKEY).expect("default parses");
        assert_eq!(s.key, Code::KeyS);
        assert!(s.mods.contains(Modifiers::CONTROL));
        assert!(s.mods.contains(Modifiers::SHIFT));
    }

    #[test]
    fn parse_is_case_and_space_tolerant() {
        let s = parse_shortcut("ctrl + shift + u").expect("parses");
        assert_eq!(s.key, Code::KeyU);
        assert!(s.mods.contains(Modifiers::CONTROL));
        assert!(s.mods.contains(Modifiers::SHIFT));
    }

    #[test]
    fn parse_supports_digits_function_and_alt() {
        let s = parse_shortcut("Alt+F4").expect("parses");
        assert_eq!(s.key, Code::F4);
        assert!(s.mods.contains(Modifiers::ALT));
        let s = parse_shortcut("Ctrl+5").expect("parses");
        assert_eq!(s.key, Code::Digit5);
    }

    #[test]
    fn parse_rejects_modifiers_only() {
        assert!(parse_shortcut("Ctrl+Shift").is_none());
    }

    #[test]
    fn parse_rejects_unknown_key() {
        assert!(parse_shortcut("Ctrl+NoSuchKey").is_none());
    }

    #[test]
    fn parse_rejects_two_keys() {
        assert!(parse_shortcut("Ctrl+A+B").is_none());
    }
}
