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

#[cfg(test)]
mod tests {
    use super::*;
    use image::Rgba;

    /// A fixture in which every pixel carries its own coordinates, so a crop
    /// that reads the wrong row, the wrong column, or a stride taken from the
    /// crop instead of the source lands on a value no rectangle explains.
    fn located(width: u32, height: u32) -> CapturedImage {
        let mut buffer = image::RgbaImage::new(width, height);
        for (x, y, pixel) in buffer.enumerate_pixels_mut() {
            *pixel = Rgba([x as u8, y as u8, 0x5a, 0xff]);
        }
        CapturedImage {
            width,
            height,
            rgba: buffer.into_raw(),
        }
    }

    /// The coordinates every pixel of a capture claims to sit at, in reading
    /// order, which is row by row.
    fn claimed_positions(captured: &CapturedImage) -> Vec<(u8, u8)> {
        captured
            .to_dynamic_image()
            .expect("a crop is always a decodable buffer")
            .to_rgba8()
            .pixels()
            .map(|pixel| (pixel.0[0], pixel.0[1]))
            .collect()
    }

    #[test]
    fn a_selection_is_cut_from_the_pixels_underneath_it() {
        let cropped = located(8, 4).crop(2, 1, 3, 2).expect("inside the image");
        assert_eq!((cropped.width, cropped.height), (3, 2));
        assert_eq!(
            claimed_positions(&cropped),
            [(2, 1), (3, 1), (4, 1), (2, 2), (3, 2), (4, 2)]
        );
    }

    /// The origin is the one thing clamping cannot rescue. A selection that
    /// starts outside the image has no pixel to anchor to, and it is refused
    /// before its size is looked at, so a release at the far corner reports the
    /// edge rather than an empty selection the user cannot act on.
    #[test]
    fn a_selection_that_starts_past_the_edge_is_refused() {
        let source = located(8, 4);
        for (x, y) in [(8, 0), (0, 4), (99, 99), (8, 0)] {
            assert_eq!(
                source.crop(x, y, 1, 1).err(),
                Some("selection outside image bounds".to_string()),
                "a selection starting at {x},{y} is outside an 8x4 image"
            );
        }
        assert_eq!(
            source.crop(8, 0, 0, 0).err(),
            Some("selection outside image bounds".to_string()),
            "an origin outside the image outranks its own empty size"
        );
    }

    /// A drag is reported in the coordinates of the resized selection window,
    /// so rounding can put the release a pixel or two past the true edge. That
    /// costs the user nothing: the overshoot is dropped and the selection that
    /// does exist is cut.
    #[test]
    fn a_selection_that_runs_past_the_edge_keeps_the_part_that_is_there() {
        let source = located(8, 4);

        let at_the_corner = source.crop(6, 3, 40, 40).expect("clamped");
        assert_eq!((at_the_corner.width, at_the_corner.height), (2, 1));
        assert_eq!(claimed_positions(&at_the_corner), [(6, 3), (7, 3)]);

        let across_the_bottom = source.crop(5, 2, 40, 40).expect("clamped");
        assert_eq!((across_the_bottom.width, across_the_bottom.height), (3, 2));
        assert_eq!(
            claimed_positions(&across_the_bottom),
            [(5, 2), (6, 2), (7, 2), (5, 3), (6, 3), (7, 3)]
        );
    }

    #[test]
    fn a_selection_with_no_area_is_refused() {
        let source = located(8, 4);
        for (width, height) in [(0, 4), (8, 0), (0, 0)] {
            assert_eq!(
                source.crop(0, 0, width, height).err(),
                Some("empty selection".to_string()),
                "a {width}x{height} selection covers nothing"
            );
        }
    }

    /// The window's own region, which is the shape a capture is asked for most
    /// often and the one case where the copy is the whole buffer.
    #[test]
    fn a_selection_covering_the_image_returns_every_pixel() {
        let source = located(8, 4);
        let whole = source
            .crop(0, 0, 8, 4)
            .expect("the whole image is inside it");
        assert_eq!((whole.width, whole.height), (8, 4));
        assert_eq!(whole.rgba, source.rgba);
    }

    /// A crop is copied a row at a time, so a band as wide as the image is the
    /// case where a stride measured from the crop rather than the source stops
    /// lining up with the rows underneath it.
    #[test]
    fn a_band_as_wide_as_the_image_reads_its_own_rows() {
        let cropped = located(8, 4).crop(0, 1, 8, 2).expect("inside the image");
        assert_eq!((cropped.width, cropped.height), (8, 2));
        assert_eq!(
            claimed_positions(&cropped),
            [
                (0, 1),
                (1, 1),
                (2, 1),
                (3, 1),
                (4, 1),
                (5, 1),
                (6, 1),
                (7, 1),
                (0, 2),
                (1, 2),
                (2, 2),
                (3, 2),
                (4, 2),
                (5, 2),
                (6, 2),
                (7, 2),
            ]
        );
    }

    /// The mirror of that case: one pixel of every row, where a copy that
    /// advanced by the crop's own width would walk across the image instead of
    /// down it.
    #[test]
    fn a_column_one_pixel_wide_reads_down_the_image_rather_than_across_it() {
        let cropped = located(8, 4).crop(3, 0, 1, 4).expect("inside the image");
        assert_eq!((cropped.width, cropped.height), (1, 4));
        assert_eq!(
            claimed_positions(&cropped),
            [(3, 0), (3, 1), (3, 2), (3, 3)]
        );
    }

    #[test]
    fn a_one_pixel_image_crops_to_that_pixel() {
        let cropped = located(1, 1).crop(0, 0, 1, 1).expect("the only pixel");
        assert_eq!((cropped.width, cropped.height), (1, 1));
        assert_eq!(claimed_positions(&cropped), [(0, 0)]);
    }

    /// The buffer and the dimensions travel across the IPC boundary as two
    /// separate fields, so nothing stops a capture arriving with a body that
    /// does not fill the frame it claims. OCR is handed the image, and a
    /// silently misread buffer would be recognised as whatever bytes it
    /// happened to contain, so the mismatch is refused and the two numbers that
    /// disagree are named.
    #[test]
    fn a_buffer_that_does_not_fill_its_dimensions_is_refused() {
        let mut short = located(8, 4);
        short.rgba.truncate(short.rgba.len() - 4);

        let error = short
            .to_dynamic_image()
            .expect_err("a buffer short of its frame cannot be decoded");
        assert!(
            error.contains("8x4") && error.contains("does not match"),
            "the error has to name the dimensions that disagree: {error}"
        );
    }

    #[test]
    fn a_capture_round_trips_into_the_image_ocr_is_handed() {
        let source = located(3, 2);
        let decoded = source.to_dynamic_image().expect("the fixture decodes");
        assert_eq!((decoded.width(), decoded.height()), (3, 2));
        assert_eq!(decoded.to_rgba8().into_raw(), source.rgba);
    }
}
