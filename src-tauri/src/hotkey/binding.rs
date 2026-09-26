use anyhow::Result;

use super::Action;
use super::dbus::{self, Reply, ShortcutList};
use super::status::{self, Backend, HotkeyStatus};

pub const CAPTURE_ID: &str = "goat_capture";
pub const SELECT_ID: &str = "goat_region_select";

const CAPTURE_DESCRIPTION: &str = "Capture the screen and read the text with GOaT";
const SELECT_DESCRIPTION: &str = "Select a screen region and read the text with GOaT";

pub fn requested() -> Vec<(String, dbus::Options)> {
    // `preferred_trigger` is deliberately left out. The spec marks it optional
    // and defers its format to a shortcuts XDG specification that does not
    // define a trigger string we can rely on, so the portal is asked to prompt
    // the user for one instead of guessing a format.
    vec![
        (
            CAPTURE_ID.to_string(),
            dbus::options(&[("description", CAPTURE_DESCRIPTION)]),
        ),
        (
            SELECT_ID.to_string(),
            dbus::options(&[("description", SELECT_DESCRIPTION)]),
        ),
    ]
}

/// The bind reply is allowed to be a subset of the request, the empty set
/// included, and a returned id can still carry no trigger. Both mean the same
/// thing to the user — the hotkey will never fire — so both are reported.
pub fn binding_warning(bound: &ShortcutList) -> String {
    let missing: Vec<&str> = [CAPTURE_ID, SELECT_ID]
        .into_iter()
        .filter(|id| !has_trigger(bound, id))
        .collect();
    if missing.is_empty() {
        return String::new();
    }
    format!(
        "Your desktop accepted the shortcut request but bound no trigger for {}. Either its \
         permission prompt was declined or another application already owns those triggers. \
         Assign one in your system keyboard-shortcut settings, or press \"Save hotkey\" in GOaT \
         to choose one.",
        missing.join(" and ")
    )
}

pub fn describe(bound: &ShortcutList) -> String {
    let triggers: Vec<String> = bound.iter().filter_map(|(_, p)| trigger_of(p)).collect();
    if triggers.is_empty() {
        return "No trigger is assigned yet.".to_string();
    }
    format!("Desktop portal triggers: {}.", triggers.join(", "))
}

/// The status line for a portal reply, with both halves read out of that same
/// reply. A later reconfigure only ever hands over the list it received, so
/// there is no earlier verdict left to keep and a shortcut the portal still has
/// no trigger for stays in the warning.
pub fn portal_status(bound: &ShortcutList) -> HotkeyStatus {
    HotkeyStatus {
        backend: Backend::Portal,
        detail: format!(
            "{} {}",
            status::default_detail(Backend::Portal),
            describe(bound)
        ),
        warning: binding_warning(bound),
    }
}

pub fn action_for(id: &str) -> Option<Action> {
    match id {
        CAPTURE_ID => Some(Action::Capture),
        SELECT_ID => Some(Action::ScreenSelect),
        _ => None,
    }
}

pub fn id_for(action: Action) -> &'static str {
    match action {
        Action::Capture => CAPTURE_ID,
        Action::ScreenSelect => SELECT_ID,
    }
}

fn has_trigger(bound: &ShortcutList, id: &str) -> bool {
    bound
        .iter()
        .find(|(candidate, _)| candidate == id)
        .is_some_and(|(_, props)| trigger_of(props).is_some())
}

fn trigger_of(props: &Reply) -> Option<String> {
    props
        .get("trigger_description")
        .and_then(|v| v.text().ok())
        .filter(|trigger| !trigger.is_empty())
}

/// A remap can come back with the edited shortcut no longer bound, which would
/// otherwise leave the app reporting a hotkey that can never fire.
pub fn require_trigger(bound: &ShortcutList, action: Action) -> Result<()> {
    if has_trigger(bound, id_for(action)) {
        return Ok(());
    }
    Err(anyhow::anyhow!(
        "the portal no longer has a trigger for \"{}\", so it will not fire",
        id_for(action)
    ))
}

#[cfg(test)]
mod tests {
    use super::dbus::{PORTAL_PATH, SHORTCUTS_INTERFACE};
    use super::*;
    use std::collections::HashMap;
    use zbus::Message;
    use zbus::zvariant::Value;

    const CAPTURE_TRIGGER: &str = "Ctrl+Shift+S";
    const SELECT_TRIGGER: &str = "Ctrl+Shift+E";
    const BOTH_TRIGGERS: &str = "Desktop portal triggers: Ctrl+Shift+S, Ctrl+Shift+E.";

    /// The portal publishes the list on a `ShortcutsChanged` signal, so the test
    /// doubles travel as that signal and come back out of the body a real one does.
    fn list(entries: &[(&str, dbus::Options)]) -> ShortcutList {
        let shortcuts: Vec<(String, dbus::Options)> = entries
            .iter()
            .map(|(id, props)| ((*id).to_string(), props.clone()))
            .collect();
        let signal = Message::signal(PORTAL_PATH, SHORTCUTS_INTERFACE, "ShortcutsChanged")
            .and_then(|builder| builder.build(&shortcuts))
            .expect("the test signal serialises");
        signal
            .body()
            .deserialize()
            .expect("the test signal decodes")
    }

    fn with_trigger(trigger: &str) -> dbus::Options {
        dbus::options(&[("trigger_description", trigger)])
    }

    fn both_bound() -> ShortcutList {
        list(&[
            (CAPTURE_ID, with_trigger(CAPTURE_TRIGGER)),
            (SELECT_ID, with_trigger(SELECT_TRIGGER)),
        ])
    }

    /// Every shortcut the portal holds a trigger for stays out of the warning and
    /// every one it does not is named, which is the pairing a reconfigure of one
    /// shortcut used to lose.
    fn assert_names_only(warning: &str, expected: &[&str]) {
        for id in [CAPTURE_ID, SELECT_ID] {
            let unbound = expected.contains(&id);
            assert_eq!(warning.contains(id), unbound, "{id}: {warning}");
        }
    }

    #[test]
    fn warns_about_a_shortcut_the_reply_left_out() {
        let bound = list(&[(CAPTURE_ID, with_trigger(CAPTURE_TRIGGER))]);
        assert_names_only(&binding_warning(&bound), &[SELECT_ID]);
    }

    #[test]
    fn warns_when_the_reply_omits_the_trigger_option() {
        let described = dbus::options(&[("description", CAPTURE_DESCRIPTION)]);
        let bound = list(&[(CAPTURE_ID, described.clone()), (SELECT_ID, described)]);
        assert_names_only(&binding_warning(&bound), &[CAPTURE_ID, SELECT_ID]);
    }

    #[test]
    fn warns_when_the_trigger_description_is_empty() {
        let bound = list(&[
            (CAPTURE_ID, with_trigger("")),
            (SELECT_ID, with_trigger("")),
        ]);
        assert_names_only(&binding_warning(&bound), &[CAPTURE_ID, SELECT_ID]);
    }

    #[test]
    fn warns_when_the_trigger_is_not_text() {
        let not_text = HashMap::from([(String::from("trigger_description"), Value::new(7_u32))]);
        let bound = list(&[
            (CAPTURE_ID, not_text),
            (SELECT_ID, with_trigger(SELECT_TRIGGER)),
        ]);
        assert_names_only(&binding_warning(&bound), &[CAPTURE_ID]);
    }

    #[test]
    fn no_warning_when_both_shortcuts_carry_a_trigger() {
        assert!(binding_warning(&both_bound()).is_empty());
    }

    #[test]
    fn a_reconfigure_of_one_shortcut_still_reports_the_other() {
        let bound = list(&[
            (CAPTURE_ID, with_trigger(CAPTURE_TRIGGER)),
            (SELECT_ID, with_trigger("")),
        ]);
        require_trigger(&bound, Action::Capture).expect("the trigger the user just chose is bound");
        let status = portal_status(&bound);
        assert_names_only(&status.warning, &[SELECT_ID]);
        assert_eq!(
            status.detail,
            format!(
                "{} Desktop portal triggers: {CAPTURE_TRIGGER}.",
                status::default_detail(Backend::Portal)
            ),
            "the detail still names the trigger the portal does hold"
        );
    }

    #[test]
    fn a_bound_trigger_passes_the_reconfigure_check() {
        let bound = list(&[(CAPTURE_ID, with_trigger(CAPTURE_TRIGGER))]);
        require_trigger(&bound, Action::Capture).expect("a bound trigger is accepted");
    }

    #[test]
    fn a_shortcut_without_a_trigger_fails_the_reconfigure_check() {
        let bound = list(&[(SELECT_ID, with_trigger(""))]);
        let refused = require_trigger(&bound, Action::ScreenSelect)
            .expect_err("a shortcut with no trigger cannot fire");
        assert!(
            refused.to_string().contains(SELECT_ID),
            "the refusal names the shortcut that lost its trigger: {refused}"
        );
    }

    #[test]
    fn describes_a_reply_without_any_trigger() {
        assert_eq!(
            describe(&ShortcutList::new()),
            "No trigger is assigned yet."
        );
    }

    #[test]
    fn describes_the_triggers_the_reply_carries() {
        assert_eq!(describe(&both_bound()), BOTH_TRIGGERS);
    }
}
