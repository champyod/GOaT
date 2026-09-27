/// The dialog the desktop refused to open is still the better path, and the retry
/// is the user's own click, so this line is named once and opens the guidance of
/// every desktop that has such a dialog. It is left out for a desktop with no
/// dialog, because the sentence under it there says the dialog does not exist.
const PREFERRED_PATH: &str = "Press Choose again to let the desktop open its shortcut \
     dialog — that is the preferred path.";

/// Where a trigger can still be bound when the session runs a desktop this build
/// has no line of its own for. It names no settings app on purpose: a path
/// invented for a desktop it cannot vouch for sends the user to a window that is
/// not there, and the build does not know which app that desktop opens.
const GENERIC_PATH: &str = "the desktop did not open its shortcut dialog, so no trigger \
     can be chosen from here. Open your desktop's keyboard-shortcut settings, bind the GOaT \
     entries there, then press Save again";

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

/// Puts the instruction in front of the place a trigger can still be bound.
fn retry_first(manual: &str) -> String {
    format!("{PREFERRED_PATH} {manual}")
}

/// What a desktop that never opened its dialog leaves the user: the retry, then
/// the place that desktop itself binds a trigger in, named the way it names its
/// own settings so the user is not left translating a path. A compositor that
/// ships no portal is sent to its own config without the retry, because it has
/// no dialog for that retry to reopen. Pure in the desktop name, so every branch
/// is reached without a session behind it.
fn guidance_for(desktop: &str) -> String {
    if desktop.contains("kde") {
        return retry_first(
            "System Settings → Keyboard → Shortcuts — look for the GOaT entries \
             (goat_capture, goat_region_select) and assign keys there.",
        );
    }
    if desktop.contains("gnome") {
        return retry_first(
            "Settings → Keyboard → View and Customize Shortcuts — look for the GOaT \
             entries and assign keys there.",
        );
    }
    if is_portalless(desktop) {
        return "this compositor has no shortcuts-portal dialog; bind keys in its own \
         config and use the in-app Capture/Select buttons"
            .to_owned();
    }
    retry_first(GENERIC_PATH)
}

/// The desktop that owns the session, as the first entry of
/// `XDG_CURRENT_DESKTOP` lowercased: that variable holds a colon-separated list
/// in preference order, so only its first entry is the desktop that opened the
/// window, and a session that sets none of them is one with no name to read.
fn current_desktop() -> Option<String> {
    let desktops = std::env::var("XDG_CURRENT_DESKTOP")
        .unwrap_or_default()
        .to_lowercase();
    let first = desktops.split(':').next().unwrap_or_default().trim();
    (!first.is_empty()).then(|| first.to_owned())
}

/// The guidance for the session that is actually running, which is the only
/// desktop the message can be written for.
pub(super) fn configure_guidance() -> String {
    guidance_for(&current_desktop().unwrap_or_default())
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
                guidance_for(desktop).starts_with(PREFERRED_PATH),
                "{desktop} is offered the same preferred path: {}",
                guidance_for(desktop)
            );
        }
    }

    /// The retry reopens a dialog, so a compositor that ships no portal is told
    /// to press a button that cannot do anything; the guidance has to start on
    /// the config that can be edited instead.
    #[test]
    fn a_compositor_without_a_portal_is_not_sent_to_a_dialog_it_does_not_have() {
        for desktop in PORTALLESS_DESKTOPS {
            let line = guidance_for(desktop);
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
        let kde = guidance_for("kde");
        assert!(
            kde.contains("System Settings → Keyboard → Shortcuts"),
            "KDE binds its triggers in its own window: {kde}"
        );
        assert!(
            kde.contains("goat_capture") && kde.contains("goat_region_select"),
            "both entries are named, or one of them is left unbound: {kde}"
        );
        assert!(
            guidance_for("gnome").contains("Settings → Keyboard → View and Customize Shortcuts"),
            "GNOME binds its triggers in its own window too: {}",
            guidance_for("gnome")
        );
    }

    /// A compositor that ships no portal cannot be sent to a dialog that does not
    /// exist, so its line says what is true of it and names the two ways a
    /// trigger can still be bound.
    #[test]
    fn a_compositor_without_a_portal_is_told_it_has_no_dialog() {
        for desktop in PORTALLESS_DESKTOPS {
            let line = guidance_for(desktop);
            assert!(
                line.contains("no shortcuts-portal dialog")
                    && line.contains("own config")
                    && line.contains("Capture/Select"),
                "{desktop} has no dialog to wait for and is told so: {line}"
            );
        }
    }

    /// A session running a desktop this build has no line for still has to be sent
    /// somewhere, so the last resort stands in rather than nothing being named.
    #[test]
    fn a_desktop_of_neither_family_falls_back_to_the_generic_line() {
        for desktop in ["", "mate"] {
            assert!(
                guidance_for(desktop).contains(GENERIC_PATH),
                "{desktop} has no line of its own, so the last resort stands in: {}",
                guidance_for(desktop)
            );
        }
    }
}
