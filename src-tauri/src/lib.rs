// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
mod capture;
mod models;

use std::sync::Mutex;

const DEFAULT_HOTKEY: &str = "Ctrl+Shift+S";

struct AppState {
    ocr: Mutex<Option<paddleocr_rs_onnx::OcrEngine>>,
    hotkey: Mutex<String>,
}

#[derive(Clone, serde::Serialize)]
struct ResultPayload {
    image: capture::CapturedImage,
    ocr_text: String,
    translated_text: String,
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
fn init_models(state: tauri::State<'_, AppState>) -> Result<String, String> {
    let mut guard = state.ocr.lock().map_err(|e| e.to_string())?;
    if guard.is_none() {
        let engine = models::load_ocr_engine().map_err(|e| format!("{e}"))?;
        *guard = Some(engine);
    }
    Ok("OCR models loaded".to_string())
}

#[tauri::command]
fn models_status() -> models::ModelsStatus {
    models::models_status()
}

fn ensure_engine<'a>(
    guard: &'a mut std::sync::MutexGuard<'_, Option<paddleocr_rs_onnx::OcrEngine>>,
) -> Result<&'a paddleocr_rs_onnx::OcrEngine, String> {
    if guard.is_none() {
        let engine = models::load_ocr_engine().map_err(|e| format!("{e}"))?;
        **guard = Some(engine);
    }
    guard.as_ref().ok_or_else(|| "OCR engine not loaded".to_string())
}

#[tauri::command]
fn ocr(
    state: tauri::State<'_, AppState>,
    image: capture::CapturedImage,
) -> Result<String, String> {
    let dynamic = image.to_dynamic_image()?;
    let mut guard = state.ocr.lock().map_err(|e| e.to_string())?;
    let engine = ensure_engine(&mut guard)?;
    models::run_ocr(engine, &dynamic).map_err(|e| format!("{e}"))
}

#[tauri::command]
fn translate(text: String) -> Result<String, String> {
    models::run_translate(&text).map_err(|e| format!("{e}"))
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

fn run_pipeline(app: &tauri::AppHandle, state: &tauri::State<'_, AppState>) -> Result<ResultPayload, String> {
    use tauri::Emitter;
    let image = capture::capture_primary()?;
    let dynamic = image.to_dynamic_image()?;
    let ocr_text = {
        let mut guard = state.ocr.lock().map_err(|e| e.to_string())?;
        let engine = ensure_engine(&mut guard)?;
        models::run_ocr(engine, &dynamic).map_err(|e| format!("{e}"))?
    };
    let translated_text = if ocr_text.trim().is_empty() {
        String::new()
    } else {
        models::run_translate(&ocr_text).map_err(|e| format!("{e}"))?
    };
    let payload = ResultPayload {
        image,
        ocr_text,
        translated_text,
    };
    let _ = app.emit("capture-result", &payload);
    Ok(payload)
}

#[tauri::command]
fn capture_primary(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
) -> Result<ResultPayload, String> {
    run_pipeline(&app, &state)
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
                    let state: tauri::State<'_, AppState> = app.state();
                    let _ = run_pipeline(app, &state);
                }
            })
            .build(),
    )?;

    let hotkey = parse_shortcut(DEFAULT_HOTKEY).expect("default hotkey parses");
    app.global_shortcut()
        .register(hotkey)
        .map_err(|e| tauri::Error::Anyhow(anyhow::Error::msg(e)))?;
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
    Ok(hotkey)
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
            capture_primary,
            hide_window,
            set_autostart,
            is_autostart,
            get_hotkey,
            set_hotkey
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
