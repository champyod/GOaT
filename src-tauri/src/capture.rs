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
        if x >= self.width || y >= self.height {
            return Err("selection outside image bounds".to_string());
        }
        // Clamp to the image: drag coordinates arrive in resized-display
        // space and rounding can overshoot the true edge by a pixel.
        let width = width.min(self.width - x);
        let height = height.min(self.height - y);
        if width == 0 || height == 0 {
            return Err("empty selection".to_string());
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

/// A rectangle of a monitor, in that monitor's own pixels.
struct Region {
    x: u32,
    y: u32,
    width: u32,
    height: u32,
}

/// Which monitor a capture reads from: the one the desktop calls primary, or
/// one the user picked out of the order the platform lists them in.
#[derive(Clone, Copy)]
enum MonitorSelector {
    Primary,
    Index(usize),
}

/// The one monitor a capture is aimed at. Resolved once here so every capture
/// reports the same way when the desktop has no such monitor.
fn select_monitor(selector: MonitorSelector) -> Result<Monitor, String> {
    let monitors = Monitor::all().map_err(|e| e.to_string())?;
    monitors
        .into_iter()
        .enumerate()
        .find(|(position, monitor)| match selector {
            MonitorSelector::Primary => monitor.is_primary().unwrap_or(false),
            MonitorSelector::Index(index) => *position == index,
        })
        .map(|(_, monitor)| monitor)
        .ok_or_else(|| match selector {
            MonitorSelector::Primary => "no primary monitor found".to_string(),
            MonitorSelector::Index(index) => format!("no monitor at index {index}"),
        })
}

/// Reads `region` of `monitor`, or the whole of it when no region is named, and
/// carries the pixels across as the buffer the window works with.
fn capture_from(monitor: &Monitor, region: Option<Region>) -> Result<CapturedImage, String> {
    let image = match region {
        Some(region) => monitor.capture_region(region.x, region.y, region.width, region.height),
        None => monitor.capture_image(),
    }
    .map_err(|e| e.to_string())?;
    Ok(CapturedImage {
        width: image.width(),
        height: image.height(),
        rgba: image.into_raw(),
    })
}

pub fn capture_primary() -> Result<CapturedImage, String> {
    capture_from(&select_monitor(MonitorSelector::Primary)?, None)
}

pub fn capture_monitor(index: usize) -> Result<CapturedImage, String> {
    capture_from(&select_monitor(MonitorSelector::Index(index))?, None)
}

pub fn capture_monitor_region(
    index: usize,
    x: u32,
    y: u32,
    width: u32,
    height: u32,
) -> Result<CapturedImage, String> {
    let region = Region {
        x,
        y,
        width,
        height,
    };
    capture_from(
        &select_monitor(MonitorSelector::Index(index))?,
        Some(region),
    )
}
