// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
mod appearance;
mod capture;
mod hotkey;
mod models;

use std::collections::BTreeMap;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{MutexGuard, OnceLock};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

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

/// Which capture request is the newest. Every run takes the next number as it
/// starts, and a run may only put anything on the screen while its own number is
/// still the one held here, so a request the user has already answered with a
/// newer one cannot repaint the window behind it.
static CAPTURE_GENERATION: AtomicU64 = AtomicU64::new(0);

/// One run's claim on the pipeline: the number it was handed as it started.
///
/// This is the whole of "latest request wins". Superseding decides what a run is
/// allowed to *say*, not what work it is allowed to finish — the read is already
/// inside the ONNX runtime or the translator by the time anyone can supersede it,
/// and neither takes a cancellation. A run therefore finishes the call it is in
/// and then finds itself stale, which costs the user nothing they can see.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) struct CaptureTicket(u64);

impl CaptureTicket {
    /// Claims the pipeline for a run that is starting now, superseding whatever
    /// was under way. Taken before the screen is grabbed rather than once the
    /// pixels are in hand: a grab takes a quarter of a second, and a run that
    /// claimed afterwards would be handed a number putting it ahead of a request
    /// the user actually made later.
    pub(crate) fn claim(generation: &AtomicU64) -> Self {
        Self(generation.fetch_add(1, Ordering::SeqCst) + 1)
    }

    /// Whether this run is still the one the desktop last asked for.
    pub(crate) fn is_current(&self, generation: &AtomicU64) -> bool {
        self.0 == generation.load(Ordering::SeqCst)
    }
}

/// A run that failed while it was still the current one is a failure the user has
/// to be told about. One that failed after they asked for something else is not:
/// the window is already filling with the run they asked for, and a reason from
/// the run they replaced would land on top of it.
///
/// Both outcomes are the end of this run's row, so the row is closed here rather
/// than at each call site: a superseded run took the same time as any other and
/// is one of the runs a latency file has to be able to describe.
fn failure_if_current(
    generation: &AtomicU64,
    ticket: CaptureTicket,
    reason: String,
    timing: &mut CaptureTiming,
) -> Result<Option<ResultPayload>, String> {
    if ticket.is_current(generation) {
        timing.close_failed(reason)
    } else {
        Ok(timing.close_superseded())
    }
}

/// How long the window is given to leave the screen before a grab is taken. The
/// compositor draws a frame at a time and answering a request to unmap a
/// surface is not the frame that unmap lands in, so a grab taken straight after
/// the answer still finds the window in it. 250ms covers the one or two frames a
/// Wayland compositor needs to be rid of the surface, which is the whole
/// difference between a screenshot of the region the user pointed at and a
/// screenshot of GOaT.
const GRAB_SETTLE: std::time::Duration = std::time::Duration::from_millis(250);

/// Takes every window off the screen and waits for them to actually be gone, so
/// every grab in the app is taken the same way and none of them can photograph
/// this app instead of the desktop behind it. The windows stay on top of
/// everything, so a grab taken while one is up is a screenshot of GOaT.
///
/// The windows that were on the screen are named back to the caller, because
/// this is the only moment it is known: the grab is taken with the windows
/// already hidden, so the set read afterwards is the set this put away. Only the
/// windows read as visible are named, and every window is hidden either way, so
/// a window this app could not read the state of still cannot end up in the
/// screenshot.
///
/// A refusal is reported rather than swallowed, because the grab that follows is
/// then of this app and the user has no other way to know why.
///
/// The wait is a blocking sleep rather than an await so a grab can hold the lock
/// that keeps two of them apart: a guard live across an await would make the
/// future holding it un-sendable, and a Tauri command may not be one.
///
/// The wait is stamped as its own seam rather than left inside the grab it
/// belongs to. It is a fixed cost every capture pays, so a grab that is slow for
/// a reason of its own and a grab that is slow because of this cannot be told
/// apart from the grab figure alone.
pub(crate) fn clear_the_screen(app: &tauri::AppHandle, timing: &mut CaptureTiming) -> Vec<String> {
    use tauri::Manager;
    let mut on_screen = Vec::new();
    for (label, window) in app.webview_windows() {
        if window.is_visible().unwrap_or(false) {
            on_screen.push(label);
        }
        if let Err(e) = window.hide() {
            report_error(
                app,
                "capture",
                &format!("The window could not be hidden: {e}"),
            );
        }
    }
    timing.mark(GRAB_SETTLE_ENTER_NS);
    std::thread::sleep(GRAB_SETTLE);
    timing.mark(GRAB_SETTLE_EXIT_NS);
    on_screen
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
    /// Held for as long as one grab has the window off the screen, so two grabs
    /// cannot read the screen together and photograph this app in the gap between
    /// one putting the window back and the other taking it away again.
    pub(crate) grab: Mutex<()>,
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

/// The environment variable naming the file one JSONL row per capture is
/// appended to. Nothing is written unless it is set: an ordinary session must
/// leave the data directory as it found it, and a row per capture would
/// otherwise grow without bound in a directory nothing ever prunes.
const LATENCY_LOG_ENV: &str = "GOAT_LATENCY_LOG";

/// What a row's numbers do and do not include, carried into every row so that a
/// figure read out of one cannot be restated as something it is not.
///
/// The MT seam is the one a reader is most likely to misread: the translator is
/// built inside the seam, so `mt_ms` is a load from disk plus inference and is
/// never the cost of translating. The other two seams carry their own fixed
/// costs, which is why the grab waits and the OCR sidecar are named separately
/// rather than folded into a single per-stage number.
const LATENCY_SCOPE: &str = "one capture per row, measured inside the GOaT process; \
mt_ms wraps a CTranslate2 translator loaded from disk inside the seam, so mt_ms = mt_load_ms + mt_infer_ms and is not inference alone; \
grab_ms includes the fixed GRAB_SETTLE wait and the window being put back; \
ocr_ms includes a tesseract sidecar spawn when the bundled engine failed; \
the *_ns stamps are a monotonic clock offset from this process's own first stamp, not from any absolute time, and must never be subtracted from t_wall_unix_ms, the only field on a shared timeline";

/// The seams of one capture, named as the two stamps each is made of. Every key
/// in a row is one of these, so a row cannot carry a field this module has no
/// seam for, and a seam a run never reached is absent rather than zero.
const GRAB_ENTER_NS: &str = "grab_enter_ns";
const GRAB_EXIT_NS: &str = "grab_exit_ns";
const GRAB_SETTLE_ENTER_NS: &str = "grab_settle_enter_ns";
const GRAB_SETTLE_EXIT_NS: &str = "grab_settle_exit_ns";
const OCR_ENTER_NS: &str = "ocr_enter_ns";
const OCR_EXIT_NS: &str = "ocr_exit_ns";
const MT_ENTER_NS: &str = "mt_enter_ns";
const MT_LOAD_ENTER_NS: &str = "mt_load_enter_ns";
const MT_LOAD_EXIT_NS: &str = "mt_load_exit_ns";
const MT_INFER_ENTER_NS: &str = "mt_infer_enter_ns";
const MT_INFER_EXIT_NS: &str = "mt_infer_exit_ns";
const MT_EXIT_NS: &str = "mt_exit_ns";
const PIPELINE_EXIT_NS: &str = "pipeline_exit_ns";

/// Which of the two row shapes a line of the log is. The file carries both a
/// capture's own seams and the window's, and the two are told apart by this
/// field rather than by which keys happen to be present: a reader filtering on
/// a key it expects would silently read the wrong row type on a row that
/// happened to be short one field.
const ROW_KIND_CAPTURE: &str = "capture";
const ROW_KIND_FRONTEND: &str = "frontend";

/// What a window's stamps do and do not include, carried into every frontend row
/// on the same terms `LATENCY_SCOPE` is carried into every capture row.
///
/// The thing a reader is most likely to get wrong here is the subtraction:
/// `perf_draw_done_ms` is a `performance.now()` reading, it is not a duration,
/// and the only thing that makes it comparable with anything is
/// `perf_time_origin` beside it. It is written into the row rather than left to
/// the reader because a stamp without its origin cannot be lined up against
/// anything at all.
const FRONTEND_SCOPE: &str = "one window's stamps per row, measured with performance.now() inside the webview; \
perf_event_received_ms and perf_draw_done_ms are performance.now() readings, not durations, and are milliseconds since the webview's own time origin; \
perf_time_origin is the Unix-epoch milliseconds that origin sits at, so perf_time_origin + a reading gives the wall-clock instant of that reading and is the only way to line one of these up against a capture row's t_wall_unix_ms; \
these stamps must never be subtracted from the capture rows' *_ns fields, which are a different clock in the app's own process";

/// The capture this session has measured so far, so two rows written inside one
/// millisecond can still be told apart.
static LATENCY_SEQ: AtomicU64 = AtomicU64::new(0);

/// The clock every capture's stamps are measured against. `Instant` carries no
/// absolute value of its own, so `t_app_mono_ns` is nanoseconds since the first
/// stamp of this process: it orders captures within a session and says nothing
/// about where that session sits on the wall clock, which is what
/// `t_wall_unix_ms` is for. The two are never subtracted from one another.
static MONO_ORIGIN: OnceLock<Instant> = OnceLock::new();

fn mono_offset_ns(at: Instant) -> u64 {
    let origin = *MONO_ORIGIN.get_or_init(|| at);
    u64::try_from(at.saturating_duration_since(origin).as_nanos()).unwrap_or(u64::MAX)
}

/// The wall clock in milliseconds since the Unix epoch, the one field in a row
/// another tool reading an unrelated log can be lined up against. A host whose
/// clock reads before the epoch answers 0, and such a row is not usable for
/// correlation; the monotonic stamps beside it are unaffected by it.
fn wall_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX))
        .unwrap_or(0)
}

/// FNV-1a over the bytes of `text`, as sixteen lowercase hex digits. A row may
/// carry what was on the screen only as a length and a fingerprint, so two
/// captures of the same text can be recognised as such without the text itself
/// being written down. This is a checksum and not a digest: it identifies an
/// input, it proves nothing about it, and two different inputs can share one.
fn fnv1a64(text: &str) -> String {
    const OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut hash = OFFSET_BASIS;
    for byte in text.as_bytes() {
        hash ^= u64::from(*byte);
        hash = hash.wrapping_mul(PRIME);
    }
    format!("{hash:016x}")
}

/// Milliseconds to the microsecond. Finer than the run-to-run spread of every
/// seam measured here, so rounding here is presentation rather than measurement,
/// and it keeps a JSONL line readable without carrying a float's worth of noise
/// on every figure.
fn millis(nanos: f64) -> f64 {
    (nanos / 1e6 * 1e3).round() / 1e3
}

/// What the capture produced, as lengths and fingerprints. There is no field a
/// screen's own text could be written into, which is what keeps it out of a file
/// an operator collects and commits without anyone having to remember to redact.
#[derive(serde::Serialize)]
struct LatencyText {
    ocr_engine: String,
    ocr_chars: u32,
    ocr_fnv1a64: String,
    translated_chars: u32,
    translated_fnv1a64: String,
}

/// The interval between the stamps of the run, in milliseconds. A stage whose
/// two ends were not both stamped is absent rather than zero: half a stage is not
/// a duration, and zero is not what a stage that did not run took.
#[derive(serde::Serialize)]
struct LatencyPerf {
    pipeline_total_ms: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    grab_ms: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    grab_settle_ms: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    ocr_ms: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    mt_ms: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    mt_load_ms: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    mt_infer_ms: Option<f64>,
}

/// One capture's own clock, handed to the seams as it passes them. It is created
/// where a capture starts and closes into one JSONL row, so the row belongs to
/// the capture it was carried through and cannot be read as another one's.
#[derive(serde::Serialize)]
pub(crate) struct CaptureTiming {
    row_kind: &'static str,
    seq: u64,
    t_wall_unix_ms: u64,
    t_app_mono_ns: u64,
    latency_scope: &'static str,
    /// Every seam the run reached, as a monotonic offset from this process's
    /// first stamp. A `BTreeMap` so a row's keys come out in the same order every
    /// time and two rows can be diffed line for line.
    #[serde(flatten)]
    stamps_ns: BTreeMap<&'static str, u64>,
    perf: LatencyPerf,
    #[serde(skip_serializing_if = "Option::is_none")]
    text: Option<LatencyText>,
}

impl CaptureTiming {
    /// Opens a capture. This call is the row's `t_app_mono_ns`, so every stamp
    /// after it is an offset from the same instant and the run's total is
    /// measured against the moment the run began rather than against whenever
    /// the row happened to be serialised.
    pub(crate) fn begin() -> Self {
        let started = Instant::now();
        Self {
            row_kind: ROW_KIND_CAPTURE,
            seq: LATENCY_SEQ.fetch_add(1, Ordering::Relaxed),
            t_wall_unix_ms: wall_unix_ms(),
            t_app_mono_ns: mono_offset_ns(started),
            latency_scope: LATENCY_SCOPE,
            stamps_ns: BTreeMap::new(),
            perf: LatencyPerf {
                pipeline_total_ms: None,
                grab_ms: None,
                grab_settle_ms: None,
                ocr_ms: None,
                mt_ms: None,
                mt_load_ms: None,
                mt_infer_ms: None,
            },
            text: None,
        }
    }

    /// Stamps one end of one seam. Both ends of a seam go through this one
    /// method and a duration is the difference between them, so no figure can be
    /// produced from a start that was not read on the same side of the work it
    /// claims to measure as its end.
    pub(crate) fn mark(&mut self, seam: &'static str) {
        let at = Instant::now();
        self.stamps_ns.insert(seam, mono_offset_ns(at));
    }

    /// The interval between one seam's two stamps, in milliseconds. Both ends
    /// have to be on the row for there to be an interval: half a stage is not a
    /// duration, and zero is not what a stage that did not run took.
    fn span_ms(&self, entered: &str, left: &str) -> Option<f64> {
        let from = self.stamps_ns.get(entered)?;
        let to = self.stamps_ns.get(left)?;
        Some(millis(*to as f64 - *from as f64))
    }

    /// Closes this run's row on the way out of a failure one of its own steps
    /// produced, and hands the reason back so the caller reports it and the log
    /// gains the row in a single statement.
    ///
    /// A run that failed is one of the runs a per-stage figure is wanted for: the
    /// grab figure is what shows the grab is what failed, and a grab that failed
    /// is exactly the run whose cost nobody would otherwise have. The closure
    /// lives here rather than in each caller because a caller that had to
    /// remember would forget it on whichever path was added last.
    pub(crate) fn close_failed<T>(&mut self, reason: String) -> Result<T, String> {
        self.finish(None);
        self.append_row();
        Err(reason)
    }

    /// Closes this run's row on the way out of a run the user replaced. Such a run
    /// produces no result and reports no failure, but it did take the same time
    /// any other run took and it did read the screen, so its row is written on the
    /// same terms as every other — a file that listed only the runs that filled
    /// the window would not be a file of what the app cost.
    ///
    /// The result it hands back is the `None` the caller returns to its own
    /// window: the run it replaced owns that window now, and a payload from this
    /// one would paint a screenshot the user has already answered with a newer
    /// request.
    pub(crate) fn close_superseded(&mut self) -> Option<ResultPayload> {
        self.finish(None);
        self.append_row();
        None
    }

    /// Closes the row: the interval between each seam's stamps, and what the
    /// capture produced. `payload` is absent on a run that never got as far as
    /// one, and such a row carries no text facts at all rather than a set of
    /// zeroes that a reader would take for a blank screen.
    pub(crate) fn finish(&mut self, payload: Option<&ResultPayload>) {
        self.mark(PIPELINE_EXIT_NS);
        let perf = LatencyPerf {
            pipeline_total_ms: self
                .stamps_ns
                .get(PIPELINE_EXIT_NS)
                .map(|left| millis(*left as f64 - self.t_app_mono_ns as f64)),
            grab_ms: self.span_ms(GRAB_ENTER_NS, GRAB_EXIT_NS),
            grab_settle_ms: self.span_ms(GRAB_SETTLE_ENTER_NS, GRAB_SETTLE_EXIT_NS),
            ocr_ms: self.span_ms(OCR_ENTER_NS, OCR_EXIT_NS),
            mt_ms: self.span_ms(MT_ENTER_NS, MT_EXIT_NS),
            mt_load_ms: self.span_ms(MT_LOAD_ENTER_NS, MT_LOAD_EXIT_NS),
            mt_infer_ms: self.span_ms(MT_INFER_ENTER_NS, MT_INFER_EXIT_NS),
        };
        self.perf = perf;
        self.text = payload.map(|result| LatencyText {
            ocr_engine: result.ocr_engine.clone(),
            ocr_chars: result.ocr_text.chars().count() as u32,
            ocr_fnv1a64: fnv1a64(&result.ocr_text),
            translated_chars: result.translated_text.chars().count() as u32,
            translated_fnv1a64: fnv1a64(&result.translated_text),
        });
    }

    /// Appends one row to the file `LATENCY_LOG_ENV` names, and does nothing at
    /// all when it is unset.
    pub(crate) fn append_row(&self) {
        let Some(path) = latency_log_path() else {
            return;
        };
        if let Err(reason) = append_row_to(&path, self) {
            eprintln!(
                "GOaT: a latency row was neither encoded nor written to {} ({reason})",
                path.display()
            );
        }
    }
}

/// One JSONL row appended to one named file, shared by the two row shapes so
/// neither can be written in a way the other is not. A reader tells the shapes
/// apart on `row_kind`, so a line that broke the shared shape would be a line it
/// read as the wrong kind of run.
fn append_row_to<T: serde::Serialize>(path: &std::path::Path, row: &T) -> Result<(), String> {
    let encoded = serde_json::to_string(row).map_err(|e| e.to_string())?;
    append_line(path, &encoded).map_err(|e| e.to_string())
}

/// The file the rows go to, or nothing at all when no file was named. Blank
/// counts as unset: a value that names no file is the same as no value, and an
/// operator who exported an empty variable has not asked for a log.
///
/// The named value is an argument rather than read from the environment here, so
/// the gate can be held by a test. The process's environment is shared by every
/// test in the binary, and a gate proved by setting it would be proved by a race.
fn latency_log_path_from(named: Option<&str>) -> Option<std::path::PathBuf> {
    let named = named?.trim();
    if named.is_empty() {
        return None;
    }
    Some(std::path::PathBuf::from(named))
}

fn latency_log_path() -> Option<std::path::PathBuf> {
    latency_log_path_from(std::env::var(LATENCY_LOG_ENV).ok().as_deref())
}

/// What the window's own clock saw, as the window sends it.
///
/// `perf_time_origin` is the field the rest are read against and the reason this
/// row is a row rather than two loose numbers: `performance.now()` counts from
/// an origin that is private to the webview, and adding it to `timeOrigin` puts
/// every stamp back on the Unix timeline the capture rows are already on. It is
/// also why no stamp here is ever subtracted from a `*_ns` field beside it — the
/// two clocks are in two processes and the gap between their origins is not a
/// number either of them knows.
#[derive(serde::Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct FrontendMarks {
    perf_time_origin: f64,
    perf_event_received_ms: Option<f64>,
    perf_draw_done_ms: Option<f64>,
}

/// One window's stamps, closed into a row of the same file the capture rows go
/// to. It is a separate row rather than fields on the capture's own because the
/// two are two clocks: folding them together would put a `performance.now()`
/// reading next to an `Instant` offset with nothing saying they are not the same
/// scale, and the difference between the two would read as a stage.
#[derive(serde::Serialize)]
struct FrontendPerf {
    row_kind: &'static str,
    t_wall_unix_ms: u64,
    frontend_scope: &'static str,
    perf_time_origin: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    perf_event_received_ms: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    perf_draw_done_ms: Option<f64>,
}

impl FrontendPerf {
    /// The row the window's stamps become. `t_wall_unix_ms` is read here rather
    /// than sent by the window, because it is the app's clock reading and the
    /// window has no access to it: the row has to be dated on the same clock as
    /// the capture rows beside it or the two cannot be ordered.
    fn from_marks(marks: FrontendMarks) -> Self {
        Self {
            row_kind: ROW_KIND_FRONTEND,
            t_wall_unix_ms: wall_unix_ms(),
            frontend_scope: FRONTEND_SCOPE,
            perf_time_origin: marks.perf_time_origin,
            perf_event_received_ms: marks.perf_event_received_ms,
            perf_draw_done_ms: marks.perf_draw_done_ms,
        }
    }
}

/// Appends the window's stamps to the file `LATENCY_LOG_ENV` names, and writes
/// nothing at all when it is unset — the same gate the capture rows answer to,
/// so a run either logs both clocks or neither.
#[tauri::command]
fn record_frontend_perf(marks: FrontendMarks) {
    let Some(path) = latency_log_path() else {
        return;
    };
    let row = FrontendPerf::from_marks(marks);
    if let Err(reason) = append_row_to(&path, &row) {
        eprintln!(
            "GOaT: a frontend latency row was neither encoded nor written to {} ({reason})",
            path.display()
        );
    }
}

fn append_line(path: &std::path::Path, line: &str) -> std::io::Result<()> {
    use std::io::Write;
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    file.write_all(line.as_bytes())?;
    file.write_all(b"\n")
}

pub(crate) async fn run_pipeline(
    app: &tauri::AppHandle,
    state: &tauri::State<'_, AppState>,
    ticket: CaptureTicket,
    timing: &mut CaptureTiming,
) -> Result<Option<ResultPayload>, String> {
    let image = grab_monitor(app, state, ticket, timing)?;
    run_pipeline_with_image(app, state, image, ticket, timing).await
}

/// The windows off the screen, the pixels read, the same windows back — under one
/// lock that nothing else may hold across those three steps.
///
/// Two grabs running side by side would each put the windows back while the other
/// was still reading, and the screenshot taken in between photographs GOaT rather
/// than the desktop the user pointed at. Superseding a run does not spare it:
/// both requests were accepted before either one read the screen, and a grab that
/// is thrown away still has to be taken off the screen to be thrown away cleanly.
fn grab_exclusive(
    app: &tauri::AppHandle,
    state: &AppState,
    timing: &mut CaptureTiming,
    read: impl FnOnce() -> Result<capture::CapturedImage, String>,
) -> Result<capture::CapturedImage, String> {
    let _one_grab_at_a_time = hotkey::lock(&state.grab);
    let on_screen = clear_the_screen(app, timing);
    let image = read();
    restore_screen(app, &on_screen);
    image
}

/// Stores the screenshot a later region read is cut from. A grab that was
/// superseded while it was taking its pixels does not get to own that slot: the
/// window is showing a later run's image, and a region dragged on it has to be cut
/// from the same pixels it was drawn from or the text comes back from somewhere
/// else on the screen.
fn store_full_image(
    state: &AppState,
    generation: &AtomicU64,
    ticket: CaptureTicket,
    image: &capture::CapturedImage,
) -> Result<(), String> {
    if !ticket.is_current(generation) {
        return Ok(());
    }
    *state.full_image.lock().map_err(|e| e.to_string())? = Some(image.clone());
    Ok(())
}

/// A lock no other thread can take is one this thread poisoned, and taking it
/// back out of its poison would hand over state no writer can vouch for. The
/// read fails on the same terms the read itself does, because a screenshot
/// nothing can store is not a screenshot.
fn locked<T>(mutex: &Mutex<T>) -> Result<MutexGuard<'_, T>, String> {
    mutex.lock().map_err(|e| e.to_string())
}

/// The pixels of the whole monitor, taken the way every grab in this app is
/// taken: the window off the screen first, and long enough for it to be gone.
/// The stored screenshot is what a selection crops from, so it is written here
/// rather than by the callers that happen to be holding the image.
///
/// The grab is stamped around the whole of it, settle and window restore
/// included, so the figure a reader takes for "the screenshot" covers everything
/// between the run being asked for and the pixels being in hand.
///
/// Every way this can fail closes the row it opened. A grab that failed has still
/// cost the run its settle wait, and that wait is a fixed cost every capture pays
/// — leaving a failed grab out of the file would make the grab figures describe
/// only the grabs that worked, which is the selection a latency report cannot be
/// built from.
pub(crate) fn grab_monitor(
    app: &tauri::AppHandle,
    state: &tauri::State<'_, AppState>,
    ticket: CaptureTicket,
    timing: &mut CaptureTiming,
) -> Result<capture::CapturedImage, String> {
    timing.mark(GRAB_ENTER_NS);
    let monitor = *state.monitor.lock().map_err(|e| e.to_string())?;
    let image = match grab_exclusive(app, state, timing, || {
        capture::capture_monitor(monitor).or_else(|_| capture::capture_primary())
    }) {
        Ok(image) => image,
        Err(reason) => return timing.close_failed(reason),
    };
    if let Err(reason) = store_full_image(state, &CAPTURE_GENERATION, ticket, &image) {
        return timing.close_failed(reason);
    }
    timing.mark(GRAB_EXIT_NS);
    Ok(image)
}

/// Puts back the windows that were on the screen when the pixels were read, and
/// only those. A grab takes every window off the screen on every platform, and a
/// window that never comes back is a capture the user has no result to look at.
/// The bar goes back before the pipeline publishes the screenshot, so the window
/// returns on its bar and the picture arrives into a window that is already
/// there.
///
/// The set is the one `clear_the_screen` named rather than a list written here,
/// so a grab cannot take a window the user had open down with it and leave the
/// desktop as the capture found it. Only the bar is raised to the front among
/// them: it is the window a capture is about, and the others are put back where
/// they were rather than on top of the answer.
#[cfg(desktop)]
fn restore_screen(app: &tauri::AppHandle, on_screen: &[String]) {
    use tauri::Manager;
    for label in on_screen {
        if let Some(window) = app.get_webview_window(label) {
            let _ = window.show();
        }
    }
    if on_screen.iter().any(|label| label == "main") {
        show_main_window(app);
    }
}

#[cfg(not(desktop))]
fn restore_screen(_app: &tauri::AppHandle, _on_screen: &[String]) {}

#[tauri::command]
async fn capture_region(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    monitor: usize,
    x: u32,
    y: u32,
    width: u32,
    height: u32,
) -> Result<Option<ResultPayload>, String> {
    let ticket = CaptureTicket::claim(&CAPTURE_GENERATION);
    // The overlay the user picked in is this app's own window, so it has to be off
    // the screen on the same terms as the whole-window grab: a region read with
    // the window still up is a read of GOaT.
    // The row is opened before the pixels are read rather than after they are in
    // hand, so a grab this run could not pay for and a run the user replaced
    // before it started each land a row of their own. A run that failed before
    // the screen was read and a run that was never asked for are otherwise the
    // same silence, and a count of repeats cannot tell those apart from each other.
    let mut timing = CaptureTiming::begin();
    timing.mark(GRAB_ENTER_NS);
    let image = match grab_exclusive(&app, &state, &mut timing, || {
        capture::capture_monitor_region(monitor, x, y, width, height)
    }) {
        Ok(image) => image,
        Err(reason) => {
            return failure_if_current(&CAPTURE_GENERATION, ticket, reason, &mut timing);
        }
    };
    if let Err(reason) = store_full_image(&state, &CAPTURE_GENERATION, ticket, &image) {
        return failure_if_current(&CAPTURE_GENERATION, ticket, reason, &mut timing);
    }
    timing.mark(GRAB_EXIT_NS);
    run_pipeline_with_image(&app, &state, image, ticket, &mut timing).await
}

async fn run_pipeline_with_image(
    app: &tauri::AppHandle,
    state: &tauri::State<'_, AppState>,
    image: capture::CapturedImage,
    ticket: CaptureTicket,
    timing: &mut CaptureTiming,
) -> Result<Option<ResultPayload>, String> {
    // The latch is set before the screenshot is published rather than after, so
    // a problem raised while this first capture is still being read is already
    // allowed to open the window it would have opened after it.
    mark_captured(&CAPTURED);
    if !ticket.is_current(&CAPTURE_GENERATION) {
        return Ok(timing.close_superseded());
    }
    // Show the screenshot immediately; OCR/translate follow on the full image.
    // This is the only place a capture hands a window its pixels, and the step
    // they belong to travels with them: a window painting an image it was told
    // nothing about has to guess whether the read is under way.
    publish_progress(app, &CaptureProgress::reading(&image));
    let read = match ocr_with_fallback(app, state, &image, timing).await {
        Ok(read) => read,
        Err(reason) => {
            return failure_if_current(&CAPTURE_GENERATION, ticket, reason, timing);
        }
    };
    // The read is the only await in a run, so this is where a request the user
    // made while it was under way arrives, and where a run that is already stale
    // stops before paying for a translation nothing will report.
    if !ticket.is_current(&CAPTURE_GENERATION) {
        return Ok(timing.close_superseded());
    }
    let ocr_text = read.text;
    let ocr_engine = read.engine;
    let mut error = read.error;
    // The read is finished before the translation starts, so the text is
    // published on that boundary and a window can stop waiting on the read.
    publish_progress(app, &CaptureProgress::translating(&ocr_text));
    let translated_text = match translate_if_any(app, &ocr_text, timing) {
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
    if !ticket.is_current(&CAPTURE_GENERATION) {
        return Ok(timing.close_superseded());
    }
    publish_progress(app, &CaptureProgress::done(&payload));
    timing.finish(Some(&payload));
    timing.append_row();
    Ok(Some(payload))
}

#[tauri::command]
async fn ocr_selection(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    x: u32,
    y: u32,
    width: u32,
    height: u32,
) -> Result<Option<ResultPayload>, String> {
    let ticket = CaptureTicket::claim(&CAPTURE_GENERATION);
    // The row is opened before the stored screenshot is read rather than after
    // the pixels are in hand, so a lock this thread poisoned, a selection read
    // before any grab, and a crop of pixels that are not there each land a row of
    // their own. A run that failed before the pipeline started and a run that was
    // never asked for are otherwise the same silence, and a count of repeats
    // cannot tell those apart from each other.
    let mut timing = CaptureTiming::begin();
    let stored = state.full_image.lock().map_err(|e| e.to_string())?.clone();
    let Some(image) = stored else {
        return failure_if_current(
            &CAPTURE_GENERATION,
            ticket,
            "no screenshot yet, capture first".to_string(),
            &mut timing,
        );
    };
    let crop = match image.crop(x, y, width, height) {
        Ok(crop) => crop,
        Err(reason) => return failure_if_current(&CAPTURE_GENERATION, ticket, reason, &mut timing),
    };
    // A selection read crops pixels a grab already took, so its row carries the
    // read and the translation but no grab: there was none in this run.
    run_pipeline_with_image(&app, &state, crop, ticket, &mut timing).await
}

#[tauri::command]
async fn capture_primary(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
) -> Result<Option<ResultPayload>, String> {
    let ticket = CaptureTicket::claim(&CAPTURE_GENERATION);
    let mut timing = CaptureTiming::begin();
    run_pipeline(&app, &state, ticket, &mut timing).await
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
///
/// The seam covers the read as a whole, the fallback included: the two engines
/// are two answers to one question, and a figure that separated them would say
/// how long each engine costs rather than how long reading the screenshot took.
async fn ocr_with_fallback(
    app: &tauri::AppHandle,
    state: &tauri::State<'_, AppState>,
    image: &capture::CapturedImage,
    timing: &mut CaptureTiming,
) -> Result<OcrRead, String> {
    timing.mark(OCR_ENTER_NS);
    let dynamic = image.to_dynamic_image()?;
    let primary = locked(&state.ocr).and_then(|mut guard| {
        ensure_engine(app, &mut guard)
            .and_then(|engine| models::run_ocr(engine, &dynamic).map_err(|e| format!("{e}")))
    });
    let read = match primary {
        Ok(text) => OcrRead {
            text,
            engine: OCR_ENGINE_PRIMARY,
            error: String::new(),
        },
        Err(primary_err) => match models::run_ocr_fallback(app, &dynamic)
            .await
            .map_err(|e| format!("{e}"))
        {
            Ok(text) => OcrRead {
                text,
                engine: OCR_ENGINE_FALLBACK,
                error: String::new(),
            },
            Err(fallback_err) => OcrRead {
                text: String::new(),
                engine: OCR_ENGINE_NONE,
                error: format!("{primary_err}; fallback OCR also failed: {fallback_err}"),
            },
        },
    };
    timing.mark(OCR_EXIT_NS);
    Ok(read)
}

/// A blank read is not sent to the translator: there is nothing to translate,
/// and a translation of nothing is not a result the user could act on.
///
/// The load is stamped apart from the inference because it is the larger half
/// and it is not translation. `load_translator` builds a translator on every call
/// and holds nothing between captures, so a single number over this seam is the
/// cost of the model arriving plus the cost of using it, and a reader who is
/// told that `mt_ms` is inference will put it in the wrong column of the table.
fn translate_if_any(
    app: &tauri::AppHandle,
    ocr_text: &str,
    timing: &mut CaptureTiming,
) -> Result<String, String> {
    if ocr_text.trim().is_empty() {
        return Ok(String::new());
    }
    timing.mark(MT_ENTER_NS);
    timing.mark(MT_LOAD_ENTER_NS);
    let translator = models::load_translator(app).map_err(|e| format!("{e}"))?;
    timing.mark(MT_LOAD_EXIT_NS);
    timing.mark(MT_INFER_ENTER_NS);
    let translated = models::run_translate_with(&translator, ocr_text).map_err(|e| format!("{e}"));
    timing.mark(MT_INFER_EXIT_NS);
    timing.mark(MT_EXIT_NS);
    translated
}

#[cfg(desktop)]
pub(crate) fn show_main_window(app: &tauri::AppHandle) {
    use tauri::Manager;
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// The tray's menu: the bar, the shortcut window, and the way out. The shortcut
/// window is here because a launch with an unbound trigger opens it on its own,
/// and closing it leaves no other way back into it — a trigger has to be chosen
/// before any capture can happen, and the trigger it is chosen in is this window.
#[cfg(desktop)]
fn setup_tray(app: &tauri::AppHandle) -> tauri::Result<()> {
    use tauri::menu::{Menu, MenuItem};
    use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};

    let show_i = MenuItem::with_id(app, "show", "Show GOaT", true, None::<&str>)?;
    let setup_i = MenuItem::with_id(app, "setup", "Setup...", true, None::<&str>)?;
    let quit_i = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show_i, &setup_i, &quit_i])?;

    let mut tray = TrayIconBuilder::with_id("main-tray")
        .tooltip("GOaT")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "show" => show_main_window(app),
            "setup" => show_bind_setup(app),
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

/// The same entry a trigger takes, opened to the window as well. The bar's region
/// button is the only control that can start a selection, and it has no way in
/// here on its own, so a bar with no screenshot on it had a button that armed a
/// crop of a picture that was not there and reached nothing when the crop was
/// the one thing it could not do.
#[cfg(desktop)]
#[tauri::command]
fn enter_region_select(app: tauri::AppHandle) {
    enter_screen_select(&app);
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
            grab: Mutex::new(()),
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
            #[cfg(desktop)]
            enter_region_select,
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
            report_frontend_error,
            record_frontend_perf
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

    /// A capture started while a read is under way is a request the user made
    /// later, so it is the one the window is filling. Every run is handed a
    /// number as it starts and only the newest keeps it: a run that checked
    /// nothing here would hand back the text of a screenshot the user has already
    /// replaced, and the window would have no way to tell which of the two the
    /// user meant.
    #[test]
    fn only_the_newest_capture_run_may_speak() {
        let generation = AtomicU64::new(0);
        let first = CaptureTicket::claim(&generation);
        assert!(
            first.is_current(&generation),
            "the run that just started is the current one"
        );

        let second = CaptureTicket::claim(&generation);
        assert!(
            !first.is_current(&generation),
            "a run the user answered with a newer one may say nothing"
        );
        assert!(
            second.is_current(&generation),
            "the newer run is the one filling the window"
        );

        let third = CaptureTicket::claim(&generation);
        assert!(
            !second.is_current(&generation),
            "two requests later, the first is as stale as the one before it"
        );
        assert!(third.is_current(&generation));
    }

    /// Nothing is claimed by number alone — two runs can never hold the same one,
    /// so a claim is a decision rather than a comparison against whoever came
    /// before. The counter this is pinned against is the app's own, incremented
    /// only by `claim`, and it starts at zero so the first run a session ever
    /// makes is number one rather than zero, which `is_current` would otherwise
    /// read as current before anything had been claimed at all.
    #[test]
    fn a_claim_is_never_handed_out_twice() {
        let generation = AtomicU64::new(0);
        let mut claimed = Vec::new();
        for _ in 0..8 {
            let ticket = CaptureTicket::claim(&generation);
            assert!(
                !claimed.contains(&ticket),
                "two runs were handed the same claim: {ticket:?}"
            );
            claimed.push(ticket);
        }
        assert_eq!(claimed.len(), 8);
    }

    /// A run the user has replaced answers its own call with nothing rather than
    /// with a failure: the window it would have reported to is already filling
    /// with the run that replaced it, and a reason from the run they threw away
    /// would land on top of the result they asked for.
    #[test]
    fn a_run_the_user_replaced_reports_no_failure_and_hands_back_no_result() {
        let generation = AtomicU64::new(0);
        let replaced = CaptureTicket::claim(&generation);
        let current = CaptureTicket::claim(&generation);

        let mut discarded_row = CaptureTiming::begin();
        let discarded = failure_if_current(
            &generation,
            replaced,
            "the sidecar died".to_string(),
            &mut discarded_row,
        );
        assert!(
            matches!(discarded, Ok(None)),
            "a superseded run answers with no result rather than with a failure"
        );
        let mut reported_row = CaptureTiming::begin();
        assert_eq!(
            failure_if_current(
                &generation,
                current,
                "the sidecar died".to_string(),
                &mut reported_row,
            )
            .err(),
            Some("the sidecar died".to_string()),
            "the run still on screen reports its own failure"
        );
    }

    /// A region read is cut from the screenshot the window is showing, so the
    /// slot that screenshot is kept in belongs to the run that put it there. A
    /// grab the user replaced arrives afterwards — it takes a quarter of a second
    /// to take the screen — and would otherwise leave the slot holding pixels of
    /// an image nothing is displaying, which reads as text from somewhere else on
    /// the screen.
    #[test]
    fn a_grab_the_user_replaced_does_not_take_the_screenshot_a_later_one_stored() {
        let state = AppState {
            ocr: Mutex::new(None),
            hotkey: Mutex::new(DEFAULT_HOTKEY.to_string()),
            select_hotkey: Mutex::new(DEFAULT_SELECT_HOTKEY.to_string()),
            monitor: Mutex::new(0),
            full_image: Mutex::new(None),
            grab: Mutex::new(()),
            hotkey_status: Mutex::new(hotkey::status::initial()),
            errors: Mutex::new(Vec::new()),
        };
        let generation = AtomicU64::new(0);
        let stale = CaptureTicket::claim(&generation);
        let kept = CaptureTicket::claim(&generation);

        let shown = capture::CapturedImage {
            width: 2,
            height: 1,
            rgba: vec![1, 1, 1, 255, 2, 2, 2, 255],
        };
        let discarded = capture::CapturedImage {
            width: 1,
            height: 1,
            rgba: vec![9, 9, 9, 255],
        };

        store_full_image(&state, &generation, kept, &shown).expect("the shown one is kept");
        store_full_image(&state, &generation, stale, &discarded)
            .expect("a superseded grab stores nothing rather than failing");

        let stored = hotkey::lock(&state.full_image)
            .clone()
            .expect("a screenshot is kept");
        assert_eq!(
            stored.rgba, shown.rgba,
            "the slot holds the image on screen"
        );
    }

    /// The window's stamps cross a process boundary as JSON and nothing else
    /// checks that they survive it: a key renamed on one side is a stamp the
    /// other side reads as absent, which is indistinguishable from a run that
    /// never drew. The round trip is therefore pinned from the shape the window
    /// actually sends.
    #[test]
    fn a_frontend_row_survives_the_wire_with_its_origin_and_its_stamps() {
        let marks: FrontendMarks = serde_json::from_str(
            r#"{"perfTimeOrigin":1767225600123.5,"perfEventReceivedMs":40.25,"perfDrawDoneMs":41.5}"#,
        )
        .expect("the camelCase shape the window sends deserialises");
        assert_eq!(marks.perf_time_origin, 1767225600123.5);

        let wire = serde_json::to_value(FrontendPerf::from_marks(marks)).expect("the row encodes");
        assert_eq!(wire["row_kind"], ROW_KIND_FRONTEND);
        assert_eq!(wire["perf_time_origin"].as_f64(), Some(1767225600123.5));
        assert_eq!(wire["perf_event_received_ms"].as_f64(), Some(40.25));
        assert_eq!(wire["perf_draw_done_ms"].as_f64(), Some(41.5));
        assert!(
            wire["t_wall_unix_ms"].is_u64(),
            "the row is dated on the app's own clock, which is what orders it against a capture row: {wire}"
        );
    }

    /// A run that ended before the draw had two of the three fields and nothing
    /// standing in for the third. Written as 0 it would read as a stamp taken at
    /// the time origin, which is a reading no run produces.
    #[test]
    fn a_frontend_run_that_never_drew_carries_no_draw_stamp_rather_than_a_zero() {
        let marks: FrontendMarks = serde_json::from_str(
            r#"{"perfTimeOrigin":1767225600123.5,"perfEventReceivedMs":40.25}"#,
        )
        .expect("a run that ended early still sends what it had");

        let wire = serde_json::to_value(FrontendPerf::from_marks(marks)).expect("the row encodes");
        assert_eq!(wire["perf_event_received_ms"].as_f64(), Some(40.25));
        assert!(
            wire.get("perf_draw_done_ms").is_none(),
            "the field is left off the row rather than written as zero: {wire}"
        );
    }

    /// A key that does not match the shape the window sends is refused rather
    /// than read. The origin is what every other stamp on the row is read
    /// against, so a payload carrying one under another spelling is a row whose
    /// numbers cannot be lined up against anything — and one that would be
    /// refused at the far end anyway, after the window had sent it.
    #[test]
    fn a_stamp_the_window_misspells_is_refused_rather_than_read() {
        for payload in [
            r#"{"perf_time_origin":1767225600123.5,"perfEventReceivedMs":40.25}"#,
            r#"{"perfTimeOrgin":1767225600123.5,"perfEventReceivedMs":40.25}"#,
        ] {
            assert!(
                serde_json::from_str::<FrontendMarks>(payload).is_err(),
                "an origin the window spelled another way is not an origin: {payload}"
            );
        }
        serde_json::from_str::<FrontendMarks>(r#"{"perfTimeOrigin":1767225600123.5}"#)
            .expect("the one spelling the window sends is the one that reads");
    }

    /// The unit every figure under statistics is stated in. The rounding is to
    /// the microsecond because it is finer than the run-to-run spread of every
    /// seam measured here, and rounding half up is pinned because a figure that
    /// rounded to even would disagree with the row it came from at the last digit.
    #[test]
    fn millis_rounds_to_the_microsecond_and_rounds_half_up() {
        assert_eq!(millis(0.0), 0.0);
        assert_eq!(millis(1_000_000.0), 1.0);
        assert_eq!(millis(2_500_000.0), 2.5);
        assert_eq!(
            millis(999.0),
            0.001,
            "one nanosecond is one microsecond, not nothing"
        );
        assert_eq!(
            millis(1_500.0),
            0.002,
            "a figure halfway between two microseconds rounds up rather than sitting on the mark"
        );
    }

    /// The offset exists so two rows written inside one millisecond can still be
    /// told apart, so what has to hold is that the difference between two readings
    /// is the elapsed time between them — the shared origin cancels, which is why
    /// this is asserted without depending on which test initialised that origin.
    #[test]
    fn a_monotonic_offset_is_nanoseconds_from_one_shared_origin() {
        let first = Instant::now();
        let at_first = mono_offset_ns(first);
        let three_ms_later = mono_offset_ns(first + std::time::Duration::from_millis(3));
        assert_eq!(
            three_ms_later - at_first,
            3_000_000,
            "two readings three milliseconds apart are three million nanoseconds apart"
        );
        assert!(
            at_first <= three_ms_later,
            "the offset never goes backwards: {at_first} then {three_ms_later}"
        );
    }

    /// Half a stage is not a duration. A run that was stamped on entry and then
    /// failed reports no figure for it, because the alternative — a number derived
    /// from one end — would be a duration for work this app never timed.
    #[test]
    fn a_seam_with_only_one_end_stamped_reports_no_figure() {
        let mut timing = CaptureTiming::begin();
        timing.mark(GRAB_ENTER_NS);
        assert!(
            timing.span_ms(GRAB_ENTER_NS, GRAB_EXIT_NS).is_none(),
            "a grab that never finished has no grab figure"
        );
        assert!(
            timing
                .span_ms("a_seam_this_module_has_none_of", GRAB_ENTER_NS)
                .is_none(),
            "a seam that was never stamped at all is not the same as one that was"
        );
        timing.mark(GRAB_EXIT_NS);
        assert!(
            timing.span_ms(GRAB_ENTER_NS, GRAB_EXIT_NS).is_some(),
            "both ends on the row is what makes there an interval"
        );
    }

    /// A failed capture is one of the runs a per-stage figure is wanted for: it
    /// still paid the fixed settle wait, and a grab figure counting only the grabs
    /// that worked would be a figure of the easy cases. What it never reached is
    /// left off the row rather than written as zero, which would read as a stage
    /// that was free.
    #[test]
    fn a_failed_grab_row_keeps_the_wait_it_paid_and_leaves_the_rest_off() {
        let mut timing = CaptureTiming::begin();
        timing.mark(GRAB_ENTER_NS);
        timing.mark(GRAB_SETTLE_ENTER_NS);
        timing.mark(GRAB_SETTLE_EXIT_NS);
        timing.finish(None);

        let wire = serde_json::to_value(&timing).expect("a closed row encodes");
        assert_eq!(wire["row_kind"], ROW_KIND_CAPTURE);
        let perf = &wire["perf"];
        assert!(
            perf["pipeline_total_ms"].is_number(),
            "the run took time even though it produced nothing: {wire}"
        );
        assert!(
            perf["grab_settle_ms"].is_number(),
            "the failed grab still paid the fixed wait, and this is the figure that says so: {wire}"
        );
        for absent in ["grab_ms", "ocr_ms", "mt_ms", "mt_load_ms", "mt_infer_ms"] {
            assert!(
                perf.get(absent).is_none(),
                "a stage the run never finished is left off the row: {wire}"
            );
        }
        assert!(
            wire.get("text").is_none(),
            "a run that read nothing carries no text facts, and zeroes would read as a blank screen: {wire}"
        );
    }

    /// The gate is the environment variable, and a run either logs both clocks or
    /// neither. Blank counts as unset because a value that names no file is the
    /// same as no value; a gate that opened on whitespace would create a file
    /// whose name is spaces in every session that exported an empty variable.
    #[test]
    fn the_log_gate_is_the_named_value_and_blank_counts_as_unset() {
        assert!(
            latency_log_path_from(None).is_none(),
            "an unset variable names no file, so an ordinary session writes nothing at all"
        );
        assert!(latency_log_path_from(Some("")).is_none());
        assert!(latency_log_path_from(Some("  \t \n ")).is_none());
        assert_eq!(
            latency_log_path_from(Some("  /tmp/goat-run.jsonl  ")).as_deref(),
            Some(std::path::Path::new("/tmp/goat-run.jsonl")),
            "a path is taken as written apart from the whitespace around it"
        );
    }

    /// The gate decides whether a file exists at all, and an ordinary session must
    /// leave the data directory as it found it — a row per capture would
    /// otherwise grow without bound in a directory nothing prunes. This holds the
    /// gate shut by naming no file rather than by touching the process
    /// environment, which every test in this binary shares, and it writes through
    /// both arms so the absence below is the gate's doing rather than a writer
    /// that does nothing.
    #[test]
    fn a_gate_that_names_no_file_writes_no_row_to_it() {
        let path =
            std::env::temp_dir().join(format!("goat-latency-gate-{}.jsonl", std::process::id()));
        let _ = std::fs::remove_file(&path);
        let mut capture = CaptureTiming::begin();
        capture.mark(GRAB_ENTER_NS);
        capture.mark(GRAB_EXIT_NS);
        capture.finish(None);

        for closed in [None, Some(""), Some("   ")] {
            let opened = latency_log_path_from(closed);
            assert!(
                opened.is_none(),
                "a gate naming no file leaves nowhere to write: {closed:?}"
            );
            assert!(
                !path.exists(),
                "a closed gate creates no file, so an ordinary session writes nothing: {}",
                path.display()
            );
        }

        let opened = latency_log_path_from(path.to_str()).expect("a named path opens the gate");
        append_row_to(&opened, &capture).expect("the open gate writes");
        assert!(
            opened.exists(),
            "the same row reaches the file once a gate names it: {}",
            opened.display()
        );
        let _ = std::fs::remove_file(&path);
    }

    /// The file is what an operator collects, so a run's row has to land on a line
    /// of its own: a second run overwrites the first's row, or joins onto it, and
    /// the artefact would then hold one run's figures under another's names.
    #[test]
    fn a_row_reaches_the_file_as_one_line_and_the_next_run_does_not_overwrite_it() {
        let path =
            std::env::temp_dir().join(format!("goat-latency-append-{}.jsonl", std::process::id()));
        let _ = std::fs::remove_file(&path);
        let mut capture = CaptureTiming::begin();
        capture.mark(GRAB_ENTER_NS);
        capture.mark(GRAB_EXIT_NS);
        capture.finish(None);
        append_row_to(&path, &capture).expect("the first row is written");
        append_row_to(&path, &capture).expect("the second row is written");

        let written = std::fs::read_to_string(&path).expect("the collected file is readable");
        assert_eq!(
            written.lines().count(),
            2,
            "one line per run, rather than one file per run: {written}"
        );
        for line in written.lines() {
            let row: serde_json::Value =
                serde_json::from_str(line).expect("every line is a whole row of its own");
            assert_eq!(
                row["row_kind"], ROW_KIND_CAPTURE,
                "the field a reader filters the file on survives the file: {row}"
            );
        }
        assert!(
            written.ends_with('\n'),
            "a row without a trailing newline would join the next run onto it: {written}"
        );
        let _ = std::fs::remove_file(&path);
    }

    /// A row may carry what was on screen only as a length and a fingerprint, and
    /// that fingerprint is only useful if it is the FNV-1a-64 every other reader
    /// of the same text computes. These are the published vectors for the
    /// algorithm, so a change to the hash shows up as a change to the figures
    /// rather than as captures that quietly stopped matching.
    #[test]
    fn the_text_fingerprint_is_the_published_fnv1a64() {
        assert_eq!(fnv1a64(""), "cbf29ce484222325");
        assert_eq!(fnv1a64("a"), "af63dc4c8601ec8c");
        assert_eq!(fnv1a64("foobar"), "85944171f73967e8");
        assert_eq!(fnv1a64("hello"), "a430d84680aabd0b");
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
