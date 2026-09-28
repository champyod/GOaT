use std::fmt;

use serde::de::{self, Visitor};
use serde::{Deserialize, Deserializer, Serialize};

use crate::{AppState, capture, config_from_state, load_config, save_config};

/// The blur the bar carried before the slider existed, and the tint strength
/// that reads as a panel rather than a wash. Both are the appearance a build
/// with no appearance section shows, so they are the values a user is taken
/// back to when a field goes missing.
const DEFAULT_BLUR_PX: u8 = 14;
const DEFAULT_TINT_OPACITY: u8 = 62;

/// Which palette the window draws itself from. The names travel to the menu
/// and back as the lowercased words a select offers, and a name this build
/// does not recognise falls back to the desktop rather than refusing the file,
/// because a theme the window cannot honour is worth less than the shortcuts
/// stored beside it.
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Theme {
    #[default]
    System,
    Dark,
    Light,
}

impl<'de> Deserialize<'de> for Theme {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        deserializer.deserialize_str(ThemeName)
    }
}

struct ThemeName;

impl<'de> Visitor<'de> for ThemeName {
    type Value = Theme;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("`system`, `dark` or `light`")
    }

    fn visit_str<E>(self, name: &str) -> Result<Theme, E>
    where
        E: de::Error,
    {
        Ok(match name {
            "dark" => Theme::Dark,
            "light" => Theme::Light,
            _ => Theme::System,
        })
    }
}

/// How the user asked the window to look, as it is stored in the config file.
/// `accent` is the colour the user picked or `None` for the desktop's own, and
/// every field falls back to its default so a file written before these
/// settings existed — or one that names only the fields it knows — still loads.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct AppearanceConfig {
    pub accent: Option<String>,
    pub blur_px: u8,
    pub tint_opacity: u8,
    pub theme: Theme,
}

impl Default for AppearanceConfig {
    fn default() -> Self {
        Self {
            accent: None,
            blur_px: DEFAULT_BLUR_PX,
            tint_opacity: DEFAULT_TINT_OPACITY,
            theme: Theme::default(),
        }
    }
}

impl AppearanceConfig {
    /// The form of this block that is worth keeping. A colour input the user
    /// cleared hands over the empty string rather than the absence of a colour,
    /// and a file holding it would carry a value the styles cannot read, so it
    /// is taken here as the desktop's own colour.
    pub fn normalized(mut self) -> Self {
        if self.accent.as_ref().is_some_and(String::is_empty) {
            self.accent = None;
        }
        self
    }
}

/// Smallest size the window is drawn at, in the logical pixels the frontend
/// measures its content in. Under it the bar's own controls no longer fit side
/// by side, so a fitted size is raised to this rather than obeyed.
const MIN_WINDOW_WIDTH: f64 = 320.0;
const MIN_WINDOW_HEIGHT: f64 = 200.0;

/// The share of a screen a fitted window is allowed to take. A window sized to
/// the whole screen has no edge left to drag it by, and one dragged off the far
/// side of it cannot be brought back.
const MAX_MONITOR_SHARE: f64 = 0.9;

/// Holds a fitted content size inside the screen it is going to appear on. All
/// four numbers are in the same logical units, because a limit expressed in the
/// screen's own pixels would be a different number of points on a display that
/// draws at twice the density.
fn clamp_size(width: f64, height: f64, monitor_width: f64, monitor_height: f64) -> (f64, f64) {
    // A screen too small for the floor would put these two bounds the wrong way
    // round, and a range whose low end is above its high end is not a clamp at
    // all. The floor wins that argument: going under it would hide the bar's own
    // controls, which is the one size the window has no smaller version of.
    let widest = (monitor_width * MAX_MONITOR_SHARE).max(MIN_WINDOW_WIDTH);
    let tallest = (monitor_height * MAX_MONITOR_SHARE).max(MIN_WINDOW_HEIGHT);
    (
        width.clamp(MIN_WINDOW_WIDTH, widest),
        height.clamp(MIN_WINDOW_HEIGHT, tallest),
    )
}

/// A screen's size in its own pixels, held apart from the screen itself so the
/// choice between screens can be made and tested with no display attached.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct ScreenSize {
    width: u32,
    height: u32,
}

impl From<&tauri::Monitor> for ScreenSize {
    fn from(monitor: &tauri::Monitor) -> Self {
        Self {
            width: monitor.size().width,
            height: monitor.size().height,
        }
    }
}

/// The screen a fitted window is measured against. A window has to stay inside
/// the screen it is sitting on, so that is the screen used whenever the desktop
/// will name it; the primary screen stands in for the desktops that will not,
/// and a desktop that names no primary at all is taken at its word for the first
/// screen it lists rather than refusing to size the window.
fn screen_to_fit(
    current: Option<ScreenSize>,
    listed: &[capture::MonitorInfo],
) -> Option<ScreenSize> {
    current.or_else(|| {
        listed
            .iter()
            .find(|monitor| monitor.is_primary)
            .or_else(|| listed.first())
            .map(|monitor| ScreenSize {
                width: monitor.width,
                height: monitor.height,
            })
    })
}

/// A screen's size in the points a window is set in. A screen is measured in its
/// own pixels and a window is not, so on a display that draws at twice the
/// density the same screen is half as many points across.
fn logical_bounds(screen: ScreenSize, scale: f64) -> (f64, f64) {
    (
        f64::from(screen.width) / scale,
        f64::from(screen.height) / scale,
    )
}

/// The bounds the window is fitted inside. A scale factor the platform will not
/// report leaves the screen and the window in step rather than guessing a denser
/// one.
fn monitor_bounds(window: &tauri::WebviewWindow) -> Result<(f64, f64), String> {
    let current = window
        .current_monitor()
        .ok()
        .flatten()
        .map(|monitor| ScreenSize::from(&monitor));
    // The listed screens are only walked when the desktop cannot say where the
    // window is, so the common path costs no round trip to the display server.
    let listed = if current.is_none() {
        capture::list_monitors()?
    } else {
        Vec::new()
    };
    let screen = screen_to_fit(current, &listed)
        .ok_or_else(|| "no screen found to size the window against".to_string())?;
    Ok(logical_bounds(screen, window.scale_factor().unwrap_or(1.0)))
}

/// How the window is drawn right now, as the file holds it rather than as a
/// window would have it: this is the one part of the config no other command
/// keeps in memory, so there is nowhere else to read it from.
#[tauri::command]
pub fn get_appearance(app: tauri::AppHandle) -> AppearanceConfig {
    load_config(&app).appearance.normalized()
}

/// Records the whole look the user has settled on and tells the windows about
/// it, answering in the form that was kept so a caller can apply what it asked
/// for without a second read.
///
/// The config is rebuilt and saved whole instead of going through `persist`,
/// which fills its appearance from the file: a write that reached it would have
/// saved the value this call exists to replace.
#[tauri::command]
pub fn set_appearance(
    app: tauri::AppHandle,
    state: tauri::State<'_, AppState>,
    appearance: AppearanceConfig,
) -> Result<AppearanceConfig, String> {
    use tauri::Emitter;
    let chosen = appearance.normalized();
    let mut config = config_from_state(&app, &state);
    config.appearance = chosen.clone();
    save_config(&app, &config)?;
    let _ = app.emit("appearance-changed", &chosen);
    Ok(chosen)
}

/// Sizes the main window to the content it has just laid out. The expanded view
/// reports a size every time it reflows, so the screen's own limits are applied
/// here: a window the screen cannot show has to be caught before it is asked
/// for, because the desktop will not always refuse it.
///
/// Nothing is resized before the first capture. The window a launch puts on the
/// screen is the bar, and the size being offered here belongs to the expanded
/// view that only a capture fills, so a size that arrives before that is a
/// measurement of a body that is not there and is left unanswered rather than
/// obeyed.
#[tauri::command]
pub fn set_window_size(app: tauri::AppHandle, width: f64, height: f64) -> Result<(), String> {
    use tauri::{LogicalSize, Manager};
    if !crate::may_auto_fit() {
        return Ok(());
    }
    let Some(window) = app.get_webview_window("main") else {
        return Ok(());
    };
    let (monitor_width, monitor_height) = monitor_bounds(&window)?;
    let (width, height) = clamp_size(width, height, monitor_width, monitor_height);
    window
        .set_size(LogicalSize::new(width, height))
        .map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::UserConfig;

    fn screen(
        index: usize,
        name: &str,
        is_primary: bool,
        width: u32,
        height: u32,
    ) -> capture::MonitorInfo {
        capture::MonitorInfo {
            index,
            name: name.to_string(),
            is_primary,
            width,
            height,
            x: 0,
            y: 0,
        }
    }

    #[test]
    fn defaults_are_the_window_as_it_looks_before_these_settings_existed() {
        let config = AppearanceConfig::default();
        assert_eq!(config.accent, None);
        assert_eq!(config.blur_px, 14);
        assert_eq!(config.tint_opacity, 62);
        assert_eq!(config.theme, Theme::System);
    }

    #[test]
    fn a_config_written_before_appearance_existed_loads_the_defaults() {
        let config: UserConfig = serde_json::from_str(
            r#"{"hotkey":"Ctrl+Shift+S","select_hotkey":"Ctrl+Shift+E","monitor":0,"hide_bind_notice":false}"#,
        )
        .expect("an older config still loads");
        assert_eq!(config.appearance, AppearanceConfig::default());
    }

    #[test]
    fn a_theme_name_this_build_does_not_know_costs_the_theme_alone() {
        let config: AppearanceConfig =
            serde_json::from_str(r#"{"theme":"sepia","blur_px":4}"#).expect("loads");
        assert_eq!(config.theme, Theme::System);
        assert_eq!(config.blur_px, 4);
    }

    #[test]
    fn theme_travels_as_the_lowercase_name_the_menu_offers() {
        assert_eq!(
            serde_json::to_string(&Theme::Dark).expect("serialises"),
            "\"dark\""
        );
        let theme: Theme = serde_json::from_str("\"light\"").expect("deserialises");
        assert_eq!(theme, Theme::Light);
    }

    #[test]
    fn a_fitted_size_past_the_screen_shrinks_to_nine_tenths_of_it() {
        assert_eq!(clamp_size(5000.0, 5000.0, 1920.0, 1080.0), (1728.0, 972.0));
    }

    #[test]
    fn a_fitted_size_under_the_floor_grows_to_it() {
        assert_eq!(clamp_size(10.0, 10.0, 1920.0, 1080.0), (320.0, 200.0));
    }

    #[test]
    fn a_screen_too_small_for_the_floor_still_yields_a_size_it_can_hold() {
        assert_eq!(clamp_size(4000.0, 4000.0, 240.0, 180.0), (320.0, 200.0));
    }

    #[test]
    fn an_accent_the_user_cleared_is_the_desktops_own_colour() {
        let config = AppearanceConfig {
            accent: Some(String::new()),
            ..AppearanceConfig::default()
        };
        assert_eq!(config.normalized().accent, None);
    }

    #[test]
    fn a_chosen_accent_is_kept_as_it_was() {
        let config = AppearanceConfig {
            accent: Some("#ff8800".to_string()),
            ..AppearanceConfig::default()
        };
        assert_eq!(config.normalized().accent, Some("#ff8800".to_string()));
    }

    #[test]
    fn a_window_on_a_smaller_screen_is_fitted_to_that_screen_and_not_to_the_primary_one() {
        let listed = [
            screen(0, "Desk", true, 3840, 2160),
            screen(1, "Side", false, 1280, 800),
        ];
        let secondary = ScreenSize {
            width: 1280,
            height: 800,
        };
        assert_eq!(screen_to_fit(Some(secondary), &listed), Some(secondary));
        let (max_width, max_height) = (f64::from(secondary.width), f64::from(secondary.height));
        assert_eq!(
            clamp_size(5000.0, 5000.0, max_width, max_height),
            (1152.0, 720.0)
        );
        assert_eq!(
            screen_to_fit(None, &listed),
            Some(ScreenSize {
                width: 3840,
                height: 2160
            })
        );
    }

    #[test]
    fn a_screen_that_draws_at_twice_the_density_is_half_as_many_points_across() {
        let dense = ScreenSize {
            width: 3840,
            height: 2160,
        };
        assert_eq!(logical_bounds(dense, 2.0), (1920.0, 1080.0));
        assert_eq!(logical_bounds(dense, 1.0), (3840.0, 2160.0));
    }

    #[test]
    fn a_desktop_with_no_screen_to_name_leaves_the_window_without_bounds() {
        assert_eq!(screen_to_fit(None, &[]), None);
    }

    #[test]
    fn a_desktop_that_names_no_primary_falls_back_to_its_first_screen() {
        let listed = [
            screen(0, "Desk", false, 1920, 1080),
            screen(1, "Side", false, 1280, 800),
        ];
        assert_eq!(
            screen_to_fit(None, &listed),
            Some(ScreenSize {
                width: 1920,
                height: 1080
            })
        );
    }
}
