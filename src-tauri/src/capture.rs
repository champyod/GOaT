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

    pub fn crop(&self, x: u32, y: u32, width: u32, height: u32) -> Result<CapturedImage, String> {
        if width == 0 || height == 0 {
            return Err("empty selection".to_string());
        }
        if x.saturating_add(width) > self.width || y.saturating_add(height) > self.height {
            return Err("selection outside image bounds".to_string());
        }
        let mut rgba = Vec::with_capacity(width as usize * height as usize * 4);
        for row in y..y + height {
            let start = ((row * self.width + x) * 4) as usize;
            rgba.extend_from_slice(&self.rgba[start..start + width as usize * 4]);
        }
        Ok(CapturedImage {
            width,
            height,
            rgba,
        })
    }
}

#[derive(Clone, Serialize)]
pub struct MonitorInfo {
    pub index: usize,
    pub name: String,
    pub is_primary: bool,
    pub width: u32,
    pub height: u32,
    pub x: i32,
    pub y: i32,
}

#[tauri::command]
pub fn list_monitors() -> Result<Vec<MonitorInfo>, String> {
    let monitors = Monitor::all().map_err(|e| e.to_string())?;
    Ok(monitors
        .into_iter()
        .enumerate()
        .map(|(index, m)| MonitorInfo {
            index,
            name: m.name().unwrap_or_else(|_| format!("Monitor {index}")),
            is_primary: m.is_primary().unwrap_or(false),
            width: m.width().unwrap_or(0),
            height: m.height().unwrap_or(0),
            x: m.x().unwrap_or(0),
            y: m.y().unwrap_or(0),
        })
        .collect())
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

pub fn capture_monitor(index: usize) -> Result<CapturedImage, String> {
    let monitors = Monitor::all().map_err(|e| e.to_string())?;
    let monitor = monitors
        .into_iter()
        .nth(index)
        .ok_or_else(|| format!("no monitor at index {index}"))?;
    let image = monitor.capture_image().map_err(|e| e.to_string())?;
    Ok(CapturedImage {
        width: image.width(),
        height: image.height(),
        rgba: image.into_raw(),
    })
}

pub fn capture_monitor_region(
    index: usize,
    x: u32,
    y: u32,
    width: u32,
    height: u32,
) -> Result<CapturedImage, String> {
    let monitors = Monitor::all().map_err(|e| e.to_string())?;
    let monitor = monitors
        .into_iter()
        .nth(index)
        .ok_or_else(|| format!("no monitor at index {index}"))?;
    let image = monitor
        .capture_region(x, y, width, height)
        .map_err(|e| e.to_string())?;
    Ok(CapturedImage {
        width: image.width(),
        height: image.height(),
        rgba: image.into_raw(),
    })
}