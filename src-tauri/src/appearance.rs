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

/// The screen a fitted size is measured against, converted into the units the
/// window is set in. The primary screen is the one the app opens on and the one
/// it returns to, and a desktop that names no primary is taken at its word for
/// the first screen it lists rather than refusing to size the window at all.
fn monitor_bounds(window: &tauri::WebviewWindow) -> Result<(f64, f64), String> {
    let monitors = capture::list_monitors()?;
    let monitor = monitors
        .iter()
        .find(|monitor| monitor.is_primary)
        .or_else(|| monitors.first())
        .ok_or_else(|| "no screen found to size the window against".to_string())?;
    let scale = window.scale_factor().unwrap_or(1.0);
    Ok((
        f64::from(monitor.width) / scale,
        f64::from(monitor.height) / scale,
    ))
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
#[tauri::command]
pub fn set_window_size(app: tauri::AppHandle, width: f64, height: f64) -> Result<(), String> {
    use tauri::{LogicalSize, Manager};
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
}
