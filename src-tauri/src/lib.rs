// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
mod capture;
mod models;

use std::sync::Mutex;

struct AppState {
    ocr: Mutex<Option<models::OcrModels>>,
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(AppState {
            ocr: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            greet,
            ping,
            init_models,
            ocr,
            translate,
            capture::capture_screen
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
