use serde::Serialize;
use xcap::Monitor;

#[derive(Serialize)]
pub struct CapturedImage {
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
}

#[tauri::command]
pub fn capture_screen(x: i32, y: i32, width: u32, height: u32) -> Result<CapturedImage, String> {
    let monitors = Monitor::all().map_err(|e| e.to_string())?;
    let monitor = monitors
        .into_iter()
        .find(|m| m.is_primary().unwrap_or(false))
        .ok_or_else(|| "no primary monitor found".to_string())?;
    let image = monitor
        .capture_region(x, y, width, height)
        .map_err(|e| e.to_string())?;
    Ok(CapturedImage {
        width: image.width(),
        height: image.height(),
        rgba: image.into_raw(),
    })
}