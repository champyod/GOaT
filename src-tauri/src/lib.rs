// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
mod capture;
mod models;

use std::sync::Mutex;

const DEFAULT_HOTKEY: &str = "Ctrl+Shift+S";

struct AppState {
    ocr: Mutex<Option<pure_onnx_ocr_sync::OcrEngine>>,
    hotkey: Mutex<String>,
    monitor: Mutex<usize>,
    last_image: Mutex<Option<capture::CapturedImage>>,
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
struct UserConfig {
    hotkey: String,
    monitor: usize,
}

impl Default for UserConfig {
    fn default() -> Self {
        Self {
            hotkey: DEFAULT_HOTKEY.to_string(),
            monitor: 0,
        }
    }
}

fn config_path(app: &tauri::AppHandle) -> Result<std::path::PathBuf, String> {
    use tauri::Manager;
    let dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    Ok(dir.join("config.json"))
}

fn load_config(app: &tauri::AppHandle) -> UserConfig {
    config_path(app)
        .and_then(|p| std::fs::read_to_string(p).map_err(|e| e.to_string()))
        .and_then(|s| serde_json::from_str(&s).map_err(|e| e.to_string()))
        .unwrap_or_default()
}

fn save_config(app: &tauri::AppHandle, cfg: &UserConfig) -> Result<(), String> {
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
    guard.as_ref().ok_or_else(|| "OCR engine not loaded".to_string())
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

async fn run_pipeline(app: &tauri::AppHandle, state: &tauri::State<'_, AppState>) -> Result<ResultPayload, String> {
    let monitor = *state.monitor.lock().map_err(|e| e.to_string())?;
    let image = capture::capture_monitor(monitor).or_else(|_| capture::capture_primary())?;
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
    run_pipeline_with_image(&app, &state, image).await
}

async fn run_pipeline_with_image(app: &tauri::AppHandle, state: &tauri::State<'_, AppState>, image: capture::CapturedImage) -> Result<ResultPayload, String> {
    use tauri::Emitter;
    *state
        .last_image
        .lock()
        .map_err(|e| e.to_string())? = Some(image.clone());
    // Show the screenshot immediately; OCR/translate follow on the full image.
    let _ = app.emit("capture-image", &image);
    let dynamic = image.to_dynamic_image()?;
    let mut error = String::new();
    let ocr_text = {
        let primary: Result<String, String> = {
            let mut guard = state.ocr.lock().map_err(|e| e.to_string())?;
            ensure_engine(app, &mut guard)
                .and_then(|engine| models::run_ocr(engine, &dynamic).map_err(|e| format!("{e}")))
        };
        match primary {
            Ok(text) => text,
            Err(primary_err) => {
                match models::run_ocr_fallback(app, &dynamic).await.map_err(|e| format!("{e}")) {
                    Ok(text) => text,
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
        .last_image
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
fn show_main_window(app: &tauri::AppHandle) {
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
fn setup_shortcut(app: &tauri::AppHandle) -> tauri::Result<()> {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

    app.plugin(
        tauri_plugin_global_shortcut::Builder::new()
            .with_handler(move |app, _shortcut, event| {
                use tauri::Manager;
                if event.state == ShortcutState::Pressed {
                    show_main_window(app);
                    let app_handle = app.clone();
                    tauri::async_runtime::spawn(async move {
                        let state = app_handle.state::<AppState>();
                        let _ = run_pipeline(&app_handle, &state).await;
                    });
                }
            })
            .build(),
    )?;

    let saved = load_config(app);
    let hotkey_str = if parse_shortcut(&saved.hotkey).is_some() {
        saved.hotkey.clone()
    } else {
        DEFAULT_HOTKEY.to_string()
    };
    let hotkey = parse_shortcut(&hotkey_str).expect("hotkey parses");
    app.global_shortcut()
        .register(hotkey)
        .map_err(|e| tauri::Error::Anyhow(anyhow::Error::msg(e)))?;
    {
        use tauri::Manager;
        let state = app.state::<AppState>();
        *state.hotkey.lock().map_err(|e| tauri::Error::Anyhow(anyhow::anyhow!("{e}")))? =
            hotkey_str;
        *state.monitor.lock().map_err(|e| tauri::Error::Anyhow(anyhow::anyhow!("{e}")))? =
            saved.monitor;
    }
    Ok(())
}

fn parse_key_code(lower: &str) -> Option<tauri_plugin_global_shortcut::Code> {
    use tauri_plugin_global_shortcut::Code;
    if lower.len() == 1 {
        let c = lower.chars().next()?;
        if c.is_ascii_alphabetic() {
            return match c {
                'a' => Some(Code::KeyA),
                'b' => Some(Code::KeyB),
                'c' => Some(Code::KeyC),
                'd' => Some(Code::KeyD),
                'e' => Some(Code::KeyE),
                'f' => Some(Code::KeyF),
                'g' => Some(Code::KeyG),
                'h' => Some(Code::KeyH),
                'i' => Some(Code::KeyI),
                'j' => Some(Code::KeyJ),
                'k' => Some(Code::KeyK),
                'l' => Some(Code::KeyL),
                'm' => Some(Code::KeyM),
                'n' => Some(Code::KeyN),
                'o' => Some(Code::KeyO),
                'p' => Some(Code::KeyP),
                'q' => Some(Code::KeyQ),
                'r' => Some(Code::KeyR),
                's' => Some(Code::KeyS),
                't' => Some(Code::KeyT),
                'u' => Some(Code::KeyU),
                'v' => Some(Code::KeyV),
                'w' => Some(Code::KeyW),
                'x' => Some(Code::KeyX),
                'y' => Some(Code::KeyY),
                'z' => Some(Code::KeyZ),
                _ => None,
            };
        }
        if c.is_ascii_digit() {
            return match c {
                '0' => Some(Code::Digit0),
                '1' => Some(Code::Digit1),
                '2' => Some(Code::Digit2),
                '3' => Some(Code::Digit3),
                '4' => Some(Code::Digit4),
                '5' => Some(Code::Digit5),
                '6' => Some(Code::Digit6),
                '7' => Some(Code::Digit7),
                '8' => Some(Code::Digit8),
                '9' => Some(Code::Digit9),
                _ => None,
            };
        }
        return None;
    }
    match lower {
        "space" => Some(Code::Space),
        "enter" => Some(Code::Enter),
        "tab" => Some(Code::Tab),
        "escape" | "esc" => Some(Code::Escape),
        "backspace" => Some(Code::Backspace),
        "delete" => Some(Code::Delete),
        "up" => Some(Code::ArrowUp),
        "down" => Some(Code::ArrowDown),
        "left" => Some(Code::ArrowLeft),
        "right" => Some(Code::ArrowRight),
        "f1" => Some(Code::F1),
        "f2" => Some(Code::F2),
        "f3" => Some(Code::F3),
        "f4" => Some(Code::F4),
        "f5" => Some(Code::F5),
        "f6" => Some(Code::F6),
        "f7" => Some(Code::F7),
        "f8" => Some(Code::F8),
        "f9" => Some(Code::F9),
        "f10" => Some(Code::F10),
        "f11" => Some(Code::F11),
        "f12" => Some(Code::F12),
        _ => None,
    }
}

fn parse_shortcut(s: &str) -> Option<tauri_plugin_global_shortcut::Shortcut> {
    use tauri_plugin_global_shortcut::{Code, Modifiers, Shortcut};
    let mut mods = Modifiers::empty();
    let mut key: Option<Code> = None;
    for part in s.split('+').map(str::trim).filter(|p| !p.is_empty()) {
        let lower = part.to_ascii_lowercase();
        match lower.as_str() {
            "ctrl" | "control" => mods |= Modifiers::CONTROL,
            "shift" => mods |= Modifiers::SHIFT,
            "alt" => mods |= Modifiers::ALT,
            "super" | "win" | "meta" | "cmd" | "command" => mods |= Modifiers::SUPER,
            _ => {
                if key.is_some() {
                    return None;
                }
                key = Some(parse_key_code(&lower)?);
            }
        }
    }
    Some(Shortcut::new(
        if mods.is_empty() { None } else { Some(mods) },
        key?,
    ))
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
fn set_hotkey(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    hotkey: String,
) -> Result<String, String> {
    use tauri_plugin_global_shortcut::GlobalShortcutExt;
    let new_shortcut = parse_shortcut(&hotkey)
        .ok_or_else(|| format!("invalid shortcut \"{hotkey}\", use e.g. Ctrl+Shift+S"))?;
    let old = state
        .hotkey
        .lock()
        .map_err(|e| e.to_string())?
        .clone();
    if let Some(old_shortcut) = parse_shortcut(&old) {
        let _ = app.global_shortcut().unregister(old_shortcut);
    }
    app.global_shortcut().register(new_shortcut).map_err(|e| {
        if let Some(old_shortcut) = parse_shortcut(&old) {
            let _ = app.global_shortcut().register(old_shortcut);
        }
        format!("failed to register shortcut \"{hotkey}\": {e}")
    })?;
    *state.hotkey.lock().map_err(|e| e.to_string())? = hotkey.clone();
    let monitor = *state.monitor.lock().map_err(|e| e.to_string())?;
    save_config(
        &app,
        &UserConfig {
            hotkey: hotkey.clone(),
            monitor,
        },
    )?;
    Ok(hotkey)
}

#[tauri::command]
fn get_monitor(state: tauri::State<'_, AppState>) -> Result<usize, String> {
    state
        .monitor
        .lock()
        .map(|g| *g)
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn set_monitor(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    monitor: usize,
) -> Result<usize, String> {
    *state.monitor.lock().map_err(|e| e.to_string())? = monitor;
    let hotkey = state.hotkey.lock().map_err(|e| e.to_string())?.clone();
    save_config(&app, &UserConfig { hotkey, monitor })?;
    Ok(monitor)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .manage(AppState {
            ocr: Mutex::new(None),
            hotkey: Mutex::new(DEFAULT_HOTKEY.to_string()),
            monitor: Mutex::new(0),
            last_image: Mutex::new(None),
        })
        .setup(|app| {
            #[cfg(desktop)]
            {
                use tauri::Manager;
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.hide();
                }
                setup_tray(app.handle())?;
                setup_shortcut(app.handle())?;
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
            set_hotkey
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
