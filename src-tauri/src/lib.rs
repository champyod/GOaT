// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
mod capture;
mod hotkey;
mod models;

use std::sync::Mutex;

#[cfg(test)]
use hotkey::parse_shortcut;

pub(crate) const DEFAULT_HOTKEY: &str = "Ctrl+Shift+S";
pub(crate) const DEFAULT_SELECT_HOTKEY: &str = "Ctrl+Shift+E";

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
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
struct UserConfig {
    hotkey: String,
    #[serde(default = "default_select_hotkey")]
    select_hotkey: String,
    monitor: usize,
}

impl Default for UserConfig {
    fn default() -> Self {
        Self {
            hotkey: DEFAULT_HOTKEY.to_string(),
            select_hotkey: default_select_hotkey(),
            monitor: 0,
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
                        error = format!("{primary_err}; fallback OCR also failed: {fallback_err}");
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
    hotkey::remap(&app, hotkey::Action::Capture, &previous, &hotkey)
        .await
        .map_err(|e| format!("{e:#}"))?;
    *hotkey::lock(&state.hotkey) = hotkey.clone();
    persist(&app, &state)?;
    Ok(hotkey)
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
    hotkey::remap(&app, hotkey::Action::ScreenSelect, &previous, &hotkey)
        .await
        .map_err(|e| format!("{e:#}"))?;
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

/// Writes the whole hotkey block from live state, so every command that touches
/// one of these values persists all of them.
fn persist(app: &tauri::AppHandle, state: &tauri::State<'_, AppState>) -> Result<(), String> {
    save_config(
        app,
        &UserConfig {
            hotkey: hotkey::lock(&state.hotkey).clone(),
            select_hotkey: hotkey::lock(&state.select_hotkey).clone(),
            monitor: *hotkey::lock(&state.monitor),
        },
    )
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_notification::init())
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
        })
        .setup(|app| {
            #[cfg(desktop)]
            {
                use tauri::Manager;
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.hide();
                }
                setup_tray(app.handle())?;
                hotkey::setup(app.handle())?;
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
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
            set_autostart,
            is_autostart,
            get_hotkey,
            set_hotkey,
            get_select_hotkey,
            set_select_hotkey,
            hotkey::hotkey_status
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::*;
    use tauri_plugin_global_shortcut::{Code, Modifiers};

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
