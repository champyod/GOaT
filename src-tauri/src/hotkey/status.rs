use serde::Serialize;

const SYSTEM_DETAIL: &str = "Global hotkeys are registered with the window system.";
#[cfg(target_os = "linux")]
const PORTAL_DETAIL: &str =
    "Global hotkeys go through your desktop's shortcut portal, not the window system.";

/// How the compositor delivers a global key press to this process.
#[derive(Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Backend {
    /// The window system delivers the press itself: an X11 grab, a macOS event
    /// tap or a Windows registered hotkey.
    System,
    /// A Wayland compositor only reports input from its own surfaces and from
    /// XWayland clients, so a grab taken through the X11 connection is never
    /// delivered to a native surface. The desktop's shortcut portal is the
    /// supported way to receive a global key press on Wayland. Only Linux runs
    /// a Wayland session, so the variant does not exist on other platforms.
    #[cfg(target_os = "linux")]
    Portal,
}

#[derive(Clone, Serialize)]
pub struct HotkeyStatus {
    pub backend: Backend,
    pub detail: String,
    pub warning: String,
}

pub fn initial() -> HotkeyStatus {
    let backend = detect();
    HotkeyStatus {
        backend,
        detail: default_detail(backend).to_string(),
        warning: String::new(),
    }
}

pub fn default_detail(backend: Backend) -> &'static str {
    match backend {
        Backend::System => SYSTEM_DETAIL,
        #[cfg(target_os = "linux")]
        Backend::Portal => PORTAL_DETAIL,
    }
}

/// XWayland exports `DISPLAY` inside a Wayland session, so its presence proves
/// nothing about which toolkit owns the window. The session type and the
/// compositor socket do: either one means a Wayland compositor is driving the
/// surface, which is the only case where the portal is needed.
fn detect() -> Backend {
    #[cfg(target_os = "linux")]
    {
        let session = std::env::var("XDG_SESSION_TYPE").unwrap_or_default();
        if session.eq_ignore_ascii_case("wayland") || std::env::var_os("WAYLAND_DISPLAY").is_some()
        {
            return Backend::Portal;
        }
    }
    Backend::System
}
