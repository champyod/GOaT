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
/// no trigger for stays in the warning. The two triggers are read out of the list
/// as well, because the portal fires the keys its own dialog produced and the
/// stored shortcut names none of them. Whether a dialog can be opened at all is
/// the session's own answer rather than the reply's, so it is handed in as it
/// stands — `None` included, because a session that has not reported is not a
/// session that has answered no.
pub fn portal_status(bound: &ShortcutList, configure_supported: Option<bool>) -> HotkeyStatus {
    HotkeyStatus {
        backend: Backend::Portal,
        detail: format!(
            "{} {}",
            status::default_detail(Backend::Portal),
            describe(bound)
        ),
        warning: binding_warning(bound),
        capture_trigger: trigger_for(bound, CAPTURE_ID),
        select_trigger: trigger_for(bound, SELECT_ID),
        configure_supported,
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

/// The trigger the portal holds for one shortcut, or `None` when the list leaves
/// the id out or leaves it without one — the two shapes that both mean the
/// hotkey will never fire. Every answer about a trigger is read here, so the
/// warning, the reconfigure check and the status line cannot disagree about what
/// the portal holds.
pub(super) fn trigger_for(bound: &ShortcutList, id: &str) -> Option<String> {
    bound
        .iter()
        .find(|(candidate, _)| candidate == id)
        .and_then(|(_, props)| trigger_of(props))
}

fn has_trigger(bound: &ShortcutList, id: &str) -> bool {
    trigger_for(bound, id).is_some()
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
    /// A portal that implements the version with the dialog in it, which is the
    /// session every other test here describes.
    const DIALOG: Option<bool> = Some(true);

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
        let status = portal_status(&bound, DIALOG);
        assert_names_only(&status.warning, &[SELECT_ID]);
        assert_eq!(status.capture_trigger.as_deref(), Some(CAPTURE_TRIGGER));
        assert_eq!(
            status.select_trigger, None,
            "the shortcut the portal has no trigger for is reported as unbound"
        );
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

    /// The stored shortcut is not what the portal fires, so each row has to be
    /// told the trigger that is actually held, under its own shortcut's name.
    #[test]
    fn the_status_line_names_the_trigger_under_its_own_shortcut() {
        let status = portal_status(&both_bound(), DIALOG);
        assert_eq!(status.capture_trigger.as_deref(), Some(CAPTURE_TRIGGER));
        assert_eq!(status.select_trigger.as_deref(), Some(SELECT_TRIGGER));
    }

    /// A reconfigure can bind the shortcut the user just chose and leave the
    /// other one with nothing, so one row can be bound while the other is not.
    #[test]
    fn the_status_line_reports_each_shortcut_on_its_own() {
        let bound = list(&[
            (CAPTURE_ID, with_trigger(CAPTURE_TRIGGER)),
            (SELECT_ID, with_trigger("")),
        ]);
        let status = portal_status(&bound, DIALOG);
        assert_eq!(status.capture_trigger.as_deref(), Some(CAPTURE_TRIGGER));
        assert_eq!(status.select_trigger, None);
    }

    /// A reply with no trigger at all is the state the desktop leaves the app in
    /// when its prompt is declined, and it must reach the window as no trigger
    /// rather than as the shortcut the app asked for.
    #[test]
    fn the_status_line_reports_no_trigger_for_a_reply_without_one() {
        let status = portal_status(&ShortcutList::new(), DIALOG);
        assert_eq!(status.capture_trigger, None);
        assert_eq!(status.select_trigger, None);
    }

    /// The session's own verdict about a dialog survives every rebuild of the
    /// line, in all three of its states. A list published by the desktop's own
    /// settings, with no remap waiting for it, is a second chance to report a
    /// portal with no dialog as one that has a dialog — and to report one that
    /// never answered as one that did — and either would leave the button on a
    /// session where pressing it can only be refused.
    #[test]
    fn the_status_line_keeps_the_sessions_own_dialog_verdict() {
        for verdict in [Some(true), Some(false), None] {
            assert_eq!(
                portal_status(&both_bound(), verdict).configure_supported,
                verdict,
                "a reply that changes nothing cannot change what the session can open"
            );
        }
    }
}
