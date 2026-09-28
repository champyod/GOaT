/// The dialog the desktop refused to open is still the better path, and the retry
/// is the user's own click, so this line is named once and opens the guidance of
/// every desktop that has such a dialog. It is left out for a desktop with no
/// dialog, because the sentence under it there says the dialog does not exist —
/// and for a session whose buttons are off, because there is no click to make.
const PREFERRED_PATH: &str = "Press Choose again to let the desktop open its shortcut \
     dialog — that is the preferred path.";

/// Where a trigger can still be bound when the session runs a desktop this build
/// has no line of its own for. It names no settings app on purpose: a path
/// invented for a desktop it cannot vouch for sends the user to a window that is
/// not there, and the build does not know which app that desktop opens.
const GENERIC_PATH: &str = "the desktop did not open its shortcut dialog, so no trigger \
     can be chosen from here. Open your desktop's keyboard-shortcut settings, bind the GOaT \
     entries there, then press Save again";

/// The same last resort on a desktop that has no dialog to open at all. It stops
/// where the retry would have been, because the press that retry names is one the
/// app cannot make there — the Choose buttons are off, and telling the user to
/// use one would be the same wrong sentence one clause further on.
const GENERIC_NO_DIALOG: &str = "this desktop has no shortcut dialog to open, so no trigger \
     can be chosen from here. Open your desktop's keyboard-shortcut settings and bind the \
     GOaT entries there";

/// Desktops that name the window they bind a trigger in, so the user is not left
/// translating a path. Matched as substrings, because each desktop announces
/// itself under its own name, with that name first in preference order.
const DESKTOP_LINES: &[(&str, &str)] = &[
    (
        "kde",
        "System Settings → Keyboard → Shortcuts — look for the GOaT entries \
         (goat_capture, goat_region_select) and assign keys there.",
    ),
    (
        "gnome",
        "Settings → Keyboard → View and Customize Shortcuts — look for the GOaT \
         entries and assign keys there.",
    ),
];

/// Compositors that ship no shortcuts portal at all, matched as substrings
/// because each announces itself under its own name and the build is told which
/// compositor it is running by that name alone.
const PORTALLESS_DESKTOPS: [&str; 5] = ["sway", "wlroots", "hyprland", "wayfire", "niri"];

/// Whether this session's desktop is one of those compositors, which is the one
/// condition that keeps the retry line out of the guidance.
fn is_portalless(desktop: &str) -> bool {
    PORTALLESS_DESKTOPS
        .iter()
        .any(|name| desktop.contains(name))
}

/// Puts the instruction in front of the place a trigger can still be bound, where
/// there is a dialog for the instruction to reopen. A session with no dialog to
/// open has its Choose buttons switched off, so the retry is the one sentence
/// that cannot be said there and the manual path is the whole line.
fn retry_first(manual: &str, has_dialog: bool) -> String {
    if has_dialog {
        return format!("{PREFERRED_PATH} {manual}");
    }
    manual.to_owned()
}

/// What a desktop that never opened its dialog leaves the user: the retry, then
/// the place that desktop itself binds a trigger in, named the way it names its
/// own settings so the user is not left translating a path. `has_dialog` is
/// whether this session's portal has a dialog at all, and it is the one thing
/// that keeps the retry out of a line: on a desktop without one the buttons that
/// would reopen it are off, so a line that tells the user to press Choose again
/// describes a press that cannot happen. A compositor that ships no portal is
/// sent to its own config either way, because it has no dialog for that retry to
/// reopen whatever this build was told. Pure in both, so every branch is reached
/// without a session or a bus behind it.
fn guidance_for(desktop: &str, has_dialog: bool) -> String {
    if let Some(line) = own_settings(desktop) {
        return retry_first(line, has_dialog);
    }
    if is_portalless(desktop) {
        return "this compositor has no shortcuts-portal dialog; bind keys in its own \
         config and use the in-app Capture/Select buttons"
            .to_owned();
    }
    if !has_dialog {
        return GENERIC_NO_DIALOG.to_owned();
    }
    retry_first(GENERIC_PATH, has_dialog)
}

/// The window that desktop binds its own triggers in, or the last resort when
/// the desktop is not one the build has a line for.
fn own_settings(desktop: &str) -> Option<&'static str> {
    DESKTOP_LINES
        .iter()
        .find(|(name, _)| desktop.contains(name))
        .map(|(_, line)| *line)
}

/// The desktop that owns the session, as the first entry of
/// `XDG_CURRENT_DESKTOP` lowercased: that variable holds a colon-separated list
/// in preference order, so only its first entry is the desktop that opened the
/// window, and a session that sets none of them is one with no name to read.
fn current_desktop() -> Option<String> {
    let desktops = std::env::var("XDG_CURRENT_DESKTOP")
        .unwrap_or_default()
        .to_lowercase();
    let (first, _) = desktops.split_once(':').unwrap_or((desktops.as_str(), ""));
    let first = first.trim();
    (!first.is_empty()).then(|| first.to_owned())
}

/// The guidance for the session that is actually running, which is the only
/// desktop the message can be written for, and whether that session's portal has
/// a dialog to offer at all. A caller that has not found out passes `false` for
/// the last one: an unpressed button is the safe reading, because the retry it
/// suppresses is a sentence about a press that may not exist.
pub(super) fn configure_guidance(has_dialog: bool) -> String {
    guidance_for(&current_desktop().unwrap_or_default(), has_dialog)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A desktop with a shortcut dialog is offered the retry before any other
    /// step, on every one of them, so the same line opens the guidance of a
    /// desktop this build has no line of its own for too.
    #[test]
    fn a_desktop_with_a_dialog_is_offered_the_retry_first() {
        for desktop in ["kde", "gnome", "mate", ""] {
            assert!(
                guidance_for(desktop, true).starts_with(PREFERRED_PATH),
                "{desktop} is offered the same preferred path: {}",
                guidance_for(desktop, true)
            );
        }
    }

    /// The retry is a sentence about a press, and on a desktop with no dialog the
    /// only press it names is a button the app has switched off. Asserted on the
    /// words the user reads rather than on the branch that produced them, because
    /// the composition of a line can be right and the sentence still be wrong.
    #[test]
    fn a_desktop_with_no_dialog_is_never_sent_to_a_press_it_cannot_make() {
        for desktop in ["kde", "gnome", "mate", "sway", ""] {
            let line = guidance_for(desktop, false);
            assert!(
                !line.contains("Choose again") && !line.contains("press Save again"),
                "{desktop} is not offered a press that does nothing here: {line}"
            );
        }
    }

    /// Suppressing the retry must not leave the user with nothing: the place a
    /// trigger can still be bound is the whole line once the retry is gone, so
    /// each desktop still has to be sent somewhere it can act.
    #[test]
    fn a_desktop_with_no_dialog_is_still_told_where_to_bind() {
        for (desktop, marker) in [
            ("kde", "System Settings"),
            ("gnome", "Settings"),
            ("mate", "keyboard-shortcut settings"),
        ] {
            assert!(
                guidance_for(desktop, false).contains(marker),
                "{desktop} still names the place a trigger can be bound"
            );
        }
    }

    /// A compositor that ships no portal is told the same thing whether or not
    /// this build was told it has a dialog: it has none to reopen either way, so
    /// the verdict must not turn the retry back on for it.
    #[test]
    fn a_compositor_without_a_portal_says_the_same_thing_either_way() {
        for desktop in PORTALLESS_DESKTOPS {
            assert_eq!(
                guidance_for(desktop, false),
                guidance_for(desktop, true),
                "{desktop} has no dialog to reopen under either verdict: {}",
                guidance_for(desktop, true)
            );
        }
    }

    /// The retry reopens a dialog, so a compositor that ships no portal is told
    /// to press a button that cannot do anything; the guidance has to start on
    /// the config that can be edited instead. The words are checked rather than
    /// the branch that produced them, for the reason the no-dialog line checks
    /// them: a composed line can be right and the sentence still be wrong.
    #[test]
    fn a_compositor_without_a_portal_is_not_sent_to_a_dialog_it_does_not_have() {
        for desktop in PORTALLESS_DESKTOPS {
            let line = guidance_for(desktop, true);
            assert!(
                !line.contains("Choose again"),
                "{desktop} is never offered a dialog it does not have: {line}"
            );
            assert!(
                line.starts_with("this compositor has no shortcuts-portal dialog"),
                "{desktop} opens on what is true of it: {line}"
            );
        }
    }

    /// A desktop that names its own settings is the one case where the user can
    /// follow the line as written, so each of those has to name the window that
    /// desktop really opens, and the two triggers it has to find there.
    #[test]
    fn kde_and_gnome_name_their_own_settings_apps() {
        let kde = guidance_for("kde", true);
        assert!(
            kde.contains("System Settings → Keyboard → Shortcuts"),
            "KDE binds its triggers in its own window: {kde}"
        );
        assert!(
            kde.contains("goat_capture") && kde.contains("goat_region_select"),
            "both entries are named, or one of them is left unbound: {kde}"
        );
        assert!(
            guidance_for("gnome", true)
                .contains("Settings → Keyboard → View and Customize Shortcuts"),
            "GNOME binds its triggers in its own window too: {}",
            guidance_for("gnome", true)
        );
    }

    /// A compositor that ships no portal cannot be sent to a dialog that does not
    /// exist, so its line says what is true of it and names the two ways a
    /// trigger can still be bound.
    #[test]
    fn a_compositor_without_a_portal_is_told_it_has_no_dialog() {
        for desktop in PORTALLESS_DESKTOPS {
            let line = guidance_for(desktop, true);
            assert!(
                line.contains("no shortcuts-portal dialog")
                    && line.contains("own config")
                    && line.contains("Capture/Select"),
                "{desktop} has no dialog to wait for and is told so: {line}"
            );
        }
    }

    /// The build learns which desktop it is running by substring, so a desktop
    /// that announces itself by a longer name than the one the line is filed
    /// under still has to reach that line, and no other one.
    #[test]
    fn every_desktop_line_is_reached_by_the_name_a_desktop_announces() {
        for (name, line) in DESKTOP_LINES {
            let desktop = format!("{name}-session");
            assert_eq!(
                guidance_for(&desktop, true),
                retry_first(line, true),
                "{desktop} is sent to the {name} line and to nothing else"
            );
        }
    }

    /// A session running a desktop this build has no line for still has to be sent
    /// somewhere, so the last resort stands in rather than nothing being named.
    #[test]
    fn a_desktop_of_neither_family_falls_back_to_the_generic_line() {
        for desktop in ["", "mate"] {
            assert!(
                guidance_for(desktop, true).contains(GENERIC_PATH),
                "{desktop} has no line of its own, so the last resort stands in: {}",
                guidance_for(desktop, true)
            );
        }
    }

    /// The same desktop with no dialog gets the other last resort, and it is the
    /// one that stops where the retry was: the generic line ends in a press, and
    /// that press is one the app cannot make.
    #[test]
    fn a_desktop_of_neither_family_without_a_dialog_ends_at_the_settings() {
        for desktop in ["", "mate"] {
            assert_eq!(
                guidance_for(desktop, false),
                GENERIC_NO_DIALOG,
                "{desktop} is sent to the last resort and no further"
            );
        }
    }
}
