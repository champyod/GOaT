use serde::Serialize;

use super::Action;

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

/// The backend in use, the line the user reads, and what each shortcut is
/// actually bound to. A trigger is never read out of the stored config: a portal
/// session fires the trigger its own dialog produced and ignores anything typed,
/// so reporting the stored value there would name a shortcut no key press can
/// reach.
#[derive(Clone, Serialize)]
pub struct HotkeyStatus {
    pub backend: Backend,
    pub detail: String,
    pub warning: String,
    pub capture_trigger: Option<String>,
    pub select_trigger: Option<String>,
    /// Whether this backend can open a desktop shortcut dialog. `None` is not a
    /// "no": it is a portal session that has not reported yet, and a window that
    /// read it as one would switch its button off and show guidance about a
    /// dialog the desktop may well have.
    pub configure_supported: Option<bool>,
}

impl HotkeyStatus {
    /// Whether the backend registers the shortcut it is given, which is what
    /// makes the stored value the bound one. Only then can the saved config fill
    /// a trigger in, and only then is it read.
    pub fn binds_stored_trigger(&self) -> bool {
        matches!(self.backend, Backend::System)
    }
}

pub fn initial() -> HotkeyStatus {
    let backend = detect();
    HotkeyStatus {
        backend,
        detail: default_detail(backend).to_string(),
        warning: String::new(),
        capture_trigger: known_trigger(backend, Action::Capture),
        select_trigger: known_trigger(backend, Action::ScreenSelect),
        configure_supported: configures_dialog(backend),
    }
}

/// What a backend can be asked to open before its own session has reported back.
/// A window system binds the shortcut it is given and has no dialog to press, so
/// it can always be asked. A portal session is answered by the portal's own
/// dialog, which it may not have, and nothing is claimed for it until that portal
/// says which version of the interface it implements — the line is written before
/// that answer exists, so it crosses as "not yet known" rather than as a "no"
/// that would switch a window's button off on a desktop that has the dialog.
fn configures_dialog(backend: Backend) -> Option<bool> {
    match backend {
        Backend::System => Some(true),
        #[cfg(target_os = "linux")]
        Backend::Portal => None,
    }
}

/// What can honestly be said about a trigger before the backend has been asked.
/// A window-system session registers the string it is given, so the shortcut the
/// managed state starts from is one it holds; a portal session binds nothing
/// until its own dialog has run, and the list it answers with replaces this.
fn known_trigger(backend: Backend, action: Action) -> Option<String> {
    match backend {
        Backend::System => Some(action.example().to_string()),
        #[cfg(target_os = "linux")]
        Backend::Portal => None,
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

#[cfg(test)]
mod tests {
    use super::*;

    fn without_triggers(backend: Backend) -> HotkeyStatus {
        HotkeyStatus {
            backend,
            detail: String::new(),
            warning: String::new(),
            capture_trigger: None,
            select_trigger: None,
            configure_supported: None,
        }
    }

    /// The window system registers the string it is given, so the shortcut the
    /// app starts from is the one it holds, and it is the stored config that
    /// fills the two triggers in.
    #[test]
    fn the_window_system_starts_from_a_bound_trigger() {
        assert_eq!(
            known_trigger(Backend::System, Action::Capture).as_deref(),
            Some(crate::DEFAULT_HOTKEY)
        );
        assert_eq!(
            known_trigger(Backend::System, Action::ScreenSelect).as_deref(),
            Some(crate::DEFAULT_SELECT_HOTKEY)
        );
    }

    /// A portal session binds nothing until its own dialog has run, so a trigger
    /// reported before the first bind would name a shortcut nothing is bound to.
    #[cfg(target_os = "linux")]
    #[test]
    fn a_portal_session_binds_nothing_before_its_dialog_runs() {
        assert_eq!(known_trigger(Backend::Portal, Action::Capture), None);
        assert_eq!(known_trigger(Backend::Portal, Action::ScreenSelect), None);
    }

    /// Only the window-system backend can stand in for a trigger with the stored
    /// shortcut, and a line built before the config was read must be filled in
    /// from the managed state rather than trusted as it stands.
    #[test]
    fn only_the_window_system_binds_the_stored_shortcut() {
        assert!(without_triggers(Backend::System).binds_stored_trigger());
        #[cfg(target_os = "linux")]
        assert!(!without_triggers(Backend::Portal).binds_stored_trigger());
    }

    /// A shortcut the backend has bound none of crosses the bridge as an explicit
    /// null, so the window can tell it apart from a field that never arrived.
    #[test]
    fn a_missing_trigger_crosses_the_bridge_as_null() {
        let json =
            serde_json::to_value(without_triggers(Backend::System)).expect("the line serialises");
        assert_eq!(json["capture_trigger"], serde_json::Value::Null);
        assert_eq!(json["select_trigger"], serde_json::Value::Null);
    }

    /// A window that has to disable a button has to be able to read the verdict
    /// out of the line, so the flag is a field of its own rather than something
    /// the window has to infer from the backend and the detail. All three states
    /// are pinned here because the window acts on each of them: a "no" switches
    /// the button off, and an unknown must arrive as something it can tell from
    /// a "no" — a false written for it would look like a desktop with no dialog.
    #[test]
    fn the_dialog_verdict_crosses_the_bridge_as_itself() {
        let line = |verdict: Option<bool>| {
            serde_json::to_value(HotkeyStatus {
                configure_supported: verdict,
                ..without_triggers(Backend::System)
            })
            .expect("the line serialises")
        };
        assert_eq!(line(None)["configure_supported"], serde_json::Value::Null);
        assert_eq!(
            line(Some(true))["configure_supported"],
            serde_json::Value::Bool(true)
        );
        assert_eq!(
            line(Some(false))["configure_supported"],
            serde_json::Value::Bool(false)
        );
    }

    /// A window system binds the shortcut it is given and never asks a desktop
    /// for a dialog, so nothing about it can leave a button without a way to
    /// choose a trigger.
    #[test]
    fn a_window_system_session_can_always_open_a_dialog() {
        assert_eq!(configures_dialog(Backend::System), Some(true));
    }

    /// A portal session has no answer before that portal has named the version of
    /// its interface, and the line the window reads is written before it does.
    /// Claiming a "no" there would put a desktop that does have a dialog behind a
    /// disabled button, so the answer is withheld instead.
    #[cfg(target_os = "linux")]
    #[test]
    fn a_portal_session_claims_nothing_before_the_portal_answers() {
        assert_eq!(configures_dialog(Backend::Portal), None);
    }
}
