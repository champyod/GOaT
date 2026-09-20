use serde::{Deserialize, Serialize};
use xcap::Monitor;

#[derive(Clone, Serialize, Deserialize)]
pub struct CapturedImage {
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
}

impl CapturedImage {
    pub fn to_dynamic_image(&self) -> Result<image::DynamicImage, String> {
        image::RgbaImage::from_raw(self.width, self.height, self.rgba.clone())
            .map(image::DynamicImage::ImageRgba8)
            .ok_or_else(|| {
                format!(
                    "captured image buffer does not match {}x{}",
                    self.width, self.height
                )
            })
    }
}

#[tauri::command]
pub fn capture_screen(x: u32, y: u32, width: u32, height: u32) -> Result<CapturedImage, String> {
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

pub fn capture_primary() -> Result<CapturedImage, String> {
    let monitors = Monitor::all().map_err(|e| e.to_string())?;
    let monitor = monitors
        .into_iter()
        .find(|m| m.is_primary().unwrap_or(false))
        .ok_or_else(|| "no primary monitor found".to_string())?;
    let image = monitor.capture_image().map_err(|e| e.to_string())?;
    Ok(CapturedImage {
        width: image.width(),
        height: image.height(),
        rgba: image.into_raw(),
    })
}