// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
mod capture;
mod models;

use std::sync::Mutex;

struct AppState {
    ocr: Mutex<Option<models::OcrModels>>,
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
        let ocr_models = models::load_ocr_models().map_err(|e| format!("{e}"))?;
        *guard = Some(ocr_models);
    }
    Ok("OCR models loaded".to_string())
}

#[tauri::command]
fn ocr(state: tauri::State<'_, AppState>) -> Result<String, String> {
    let guard = state.ocr.lock().map_err(|e| e.to_string())?;
    guard
        .as_ref()
        .ok_or_else(|| "models not loaded, call init_models first".to_string())?;
    Ok("OCR placeholder".to_string())
}

#[tauri::command]
fn translate(text: String) -> Result<String, String> {
    let _translator = models::load_translator().map_err(|e| format!("{e}"))?;
    Ok(text)
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

#[tauri::command]
fn capture_primary(app: tauri::AppHandle) -> Result<ResultPayload, String> {
    use tauri::Emitter;
    let image = capture::capture_primary()?;
    let payload = ResultPayload {
        image,
        ocr_text: String::new(),
        translated_text: String::new(),
    };
    let _ = app.emit("capture-result", &payload);
    Ok(payload)
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
    use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

    let hotkey = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SHIFT), Code::KeyS);

    app.plugin(
        tauri_plugin_global_shortcut::Builder::new()
            .with_handler(move |app, shortcut, event| {
                if shortcut == &hotkey && event.state == ShortcutState::Pressed {
                    show_main_window(app);
                }
            })
            .build(),
    )?;

    app.global_shortcut()
        .register(hotkey)
        .map_err(|e| tauri::Error::Anyhow(anyhow::Error::msg(e)))?;
    Ok(())
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
            ocr,
            translate,
            capture::capture_screen,
            capture_primary,
            hide_window,
            set_autostart,
            is_autostart
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
