use std::fmt;

use serde::de::{self, Visitor};
use serde::{Deserialize, Deserializer, Serialize};

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
}
