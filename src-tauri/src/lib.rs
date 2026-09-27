// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
mod capture;
mod hotkey;
mod models;

use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(test)]
use hotkey::parse_shortcut;

pub(crate) const DEFAULT_HOTKEY: &str = "Ctrl+Shift+S";
pub(crate) const DEFAULT_SELECT_HOTKEY: &str = "Ctrl+Shift+E";

/// How many problems are kept. The log exists to explain a session that has
/// already gone wrong, and a cap keeps a failure that repeats on every key press
/// from filling memory for the life of the process.
const ERROR_LOG_CAP: usize = 100;

fn default_select_hotkey() -> String {
    DEFAULT_SELECT_HOTKEY.to_string()
}

pub(crate) struct AppState {
    pub(crate) ocr: Mutex<Option<pure_onnx_ocr_sync::OcrEngine>>,
    pub(crate) hotkey: Mutex<String>,
    pub(crate) select_hotkey: Mutex<String>,
    pub(crate) monitor: Mutex<usize>,
    pub(crate) last_image: Mutex<Option<capture::CapturedImage>>,
    pub(crate) full_image: Mutex<Option<capture::CapturedImage>>,
    pub(crate) hotkey_status: Mutex<hotkey::HotkeyStatus>,
    pub(crate) errors: Mutex<Vec<ErrorEntry>>,
}

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
}

impl Default for UserConfig {
    fn default() -> Self {
        Self {
            hotkey: DEFAULT_HOTKEY.to_string(),
            select_hotkey: default_select_hotkey(),
            monitor: 0,
            hide_bind_notice: false,
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
/// not pull the window away from the screenshot the user is working on.
#[cfg(desktop)]
fn show_error_window(app: &tauri::AppHandle) {
    use tauri::Manager;
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
fn greet(name: &str) -> String {
    format!("Hello, {}!", name)
}
#[tauri::command]
fn ping() -> String {
    "pong from Rust!".to_string()
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
fn ocr(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    image: capture::CapturedImage,
) -> Result<String, String> {
    let dynamic = image.to_dynamic_image()?;
    let mut guard = state.ocr.lock().map_err(|e| e.to_string())?;
    let engine = ensure_engine(&app, &mut guard)?;
    models::run_ocr(engine, &dynamic).map_err(|e| format!("{e}"))
}

#[tauri::command]
fn translate(app: tauri::AppHandle, text: String) -> Result<String, String> {
    models::run_translate(&app, &text).map_err(|e| format!("{e}"))
}

#[tauri::command]
fn hide_window(app: tauri::AppHandle) -> Result<(), String> {
    use tauri::Manager;
    if let Some(window) = app.get_webview_window("main") {
        window.hide().map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// The bind instructions live in the setup window, so the action bar only asks
/// for it to be raised and a window already on screen is left as it is.
#[tauri::command]
fn show_setup(app: tauri::AppHandle) {
    #[cfg(desktop)]
    show_bind_setup(&app);
}

#[tauri::command]
fn set_autostart(app: tauri::AppHandle, enabled: bool) -> Result<bool, String> {
    use tauri_plugin_autostart::ManagerExt;
    let manager = app.autolaunch();
    if enabled {
        manager.enable().map_err(|e| e.to_string())?;
    } else {
        manager.disable().map_err(|e| e.to_string())?;
    }
    manager.is_enabled().map_err(|e| e.to_string())
}

#[tauri::command]
fn is_autostart(app: tauri::AppHandle) -> Result<bool, String> {
    use tauri_plugin_autostart::ManagerExt;
    app.autolaunch().is_enabled().map_err(|e| e.to_string())
}

pub(crate) async fn run_pipeline(
    app: &tauri::AppHandle,
    state: &tauri::State<'_, AppState>,
) -> Result<ResultPayload, String> {
    let monitor = *state.monitor.lock().map_err(|e| e.to_string())?;
    let image = capture::capture_monitor(monitor).or_else(|_| capture::capture_primary())?;
    *state.full_image.lock().map_err(|e| e.to_string())? = Some(image.clone());
    run_pipeline_with_image(app, state, image).await
}

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
    let image = capture::capture_monitor_region(monitor, x, y, width, height)?;
    *state.full_image.lock().map_err(|e| e.to_string())? = Some(image.clone());
    run_pipeline_with_image(&app, &state, image).await
}

async fn run_pipeline_with_image(
    app: &tauri::AppHandle,
    state: &tauri::State<'_, AppState>,
    image: capture::CapturedImage,
) -> Result<ResultPayload, String> {
    use tauri::Emitter;
    *state.last_image.lock().map_err(|e| e.to_string())? = Some(image.clone());
    // Show the screenshot immediately; OCR/translate follow on the full image.
    let _ = app.emit("capture-image", &image);
    let dynamic = image.to_dynamic_image()?;
    let mut error = String::new();
    let mut ocr_engine = String::new();
    let ocr_text = {
        let primary: Result<String, String> = {
            let mut guard = state.ocr.lock().map_err(|e| e.to_string())?;
            ensure_engine(app, &mut guard)
                .and_then(|engine| models::run_ocr(engine, &dynamic).map_err(|e| format!("{e}")))
        };
        match primary {
            Ok(text) => {
                ocr_engine = "tract".to_string();
                text
            }
            Err(primary_err) => {
                match models::run_ocr_fallback(app, &dynamic)
                    .await
                    .map_err(|e| format!("{e}"))
                {
                    Ok(text) => {
                        ocr_engine = "tesseract".to_string();
                        text
                    }
                    Err(fallback_err) => {
                        let line =
                            format!("{primary_err}; fallback OCR also failed: {fallback_err}");
                        report_error(app, "ocr", &line);
                        error = line;
                        String::new()
                    }
                }
            }
        }
    };
    let translated_text = if ocr_text.trim().is_empty() {
        String::new()
    } else {
        match models::run_translate(app, &ocr_text).map_err(|e| format!("{e}")) {
            Ok(text) => text,
            Err(e) => {
                if error.is_empty() {
                    report_error(app, "translate", &e);
                    error = e;
                }
                String::new()
            }
        }
    };
    let payload = ResultPayload {
        image,
        ocr_text,
        translated_text,
        ocr_engine,
        error,
    };
    let _ = app.emit("capture-result", &payload);
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
/// during startup and never for one that fails later.
#[cfg(desktop)]
fn watch_unbound_triggers(app: &tauri::AppHandle) {
    use tauri::Listener;
    if load_config(app).hide_bind_notice {
        return;
    }
    let handle = app.clone();
    let _listener = app.listen("hotkey-status", move |_| {
        if triggers_unbound(&handle) {
            show_bind_setup(&handle);
        }
    });
    if triggers_unbound(app) {
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

#[tauri::command]
async fn set_hotkey(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    hotkey: String,
) -> Result<String, String> {
    let previous = hotkey::lock(&state.hotkey).clone();
    remap_trigger(&app, hotkey::Action::Capture, &previous, &hotkey).await?;
    // A backend that fires only the trigger its own dialog produced keeps the
    // one it already holds, so the field never reports a shortcut that no
    // trigger is bound to.
    if !hotkey::binds_typed_trigger(&app) {
        return Ok(previous);
    }
    *hotkey::lock(&state.hotkey) = hotkey.clone();
    persist(&app, &state)?;
    Ok(hotkey)
}

/// A trigger that cannot be reconfigured is recorded before its reason reaches
/// the window that asked for it, so the log holds the failure even when that
/// window has been closed since.
async fn remap_trigger(
    app: &tauri::AppHandle,
    action: hotkey::Action,
    previous: &str,
    next: &str,
) -> Result<(), String> {
    hotkey::remap(app, action, previous, next)
        .await
        .map_err(|e| {
            let message = format!("{e:#}");
            report_error(app, "hotkey", &message);
            message
        })
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
    let previous = hotkey::lock(&state.select_hotkey).clone();
    remap_trigger(&app, hotkey::Action::ScreenSelect, &previous, &hotkey).await?;
    if !hotkey::binds_typed_trigger(&app) {
        return Ok(previous);
    }
    *hotkey::lock(&state.select_hotkey) = hotkey.clone();
    persist(&app, &state)?;
    Ok(hotkey)
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

/// The whole config as it stands: the live hotkey block, plus the notice flag as
/// the file holds it. Reading the block from managed state and the flag from disk
/// is what keeps a write of one from putting the other's saved value back.
fn config_from_state(app: &tauri::AppHandle, state: &tauri::State<'_, AppState>) -> UserConfig {
    UserConfig {
        hotkey: hotkey::lock(&state.hotkey).clone(),
        select_hotkey: hotkey::lock(&state.select_hotkey).clone(),
        monitor: *hotkey::lock(&state.monitor),
        hide_bind_notice: load_config(app).hide_bind_notice,
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
            last_image: Mutex::new(None),
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
        })
        .invoke_handler(tauri::generate_handler![
            greet,
            ping,
            init_models,
            models_status,
            ocr,
            translate,
            capture::capture_screen,
            capture::list_monitors,
            capture_primary,
            capture_region,
            ocr_selection,
            get_monitor,
            set_monitor,
            hide_window,
            show_setup,
            set_autostart,
            is_autostart,
            get_hotkey,
            set_hotkey,
            get_select_hotkey,
            set_select_hotkey,
            get_hide_bind_notice,
            set_hide_bind_notice,
            hotkey::hotkey_status,
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
