//! Whether a dialog can be opened at all, and what a remap is told when it
//! cannot. Nothing here talks to the bus: these are the decisions a session
//! reads, and they are kept where both answers can be asked about without one.
use anyhow::{Result, anyhow};

use super::guidance;

/// The interface version that added `ConfigureShortcuts`. A portal below it has
/// no dialog to open, so no trigger can be chosen from here at all and the one
/// place a trigger can still be bound is the desktop's own settings.
const CONFIGURE_VERSION: u32 = 2;

/// Whether a portal reporting this version can be asked to open its dialog. The
/// version is the whole test: below `CONFIGURE_VERSION` the call does not exist
/// on the portal, so nothing about the session changes the answer.
pub(super) fn supports(version: u32) -> bool {
    version >= CONFIGURE_VERSION
}

/// What a remap is answered with when the portal has no dialog. It is the
/// guidance on its own, because there is no call to have refused and no dialog to
/// have gone wrong: the line that names where a trigger can still be bound is the
/// whole result, and nothing after it would run. The line is written for a
/// desktop with no dialog, so it names no press: the buttons that would make one
/// are the ones this turns away.
pub(super) fn can_configure(supports: bool) -> Result<()> {
    if supports {
        return Ok(());
    }
    Err(anyhow!("{}", guidance::configure_guidance(false)))
}

/// Puts the instruction in front of the reason, because the reason alone names a
/// transport failure the user cannot act on while the instruction is the only step
/// left. The reason is kept whole, so the diagnostics window still holds it. A
/// call that was made at all is a call on a portal that has a dialog, so the
/// retry is still true here rather than the sentence this one would replace.
pub(super) fn configure_failed(reason: &anyhow::Error) -> anyhow::Error {
    anyhow!("{}: {reason:#}", guidance::configure_guidance(true))
}

/// The guidance a window puts on screen itself, for a session whose buttons are
/// off. It is the no-dialog line for the same reason `can_configure` answers with
/// it: a window asking is a window with nothing to open, and the retry would
/// point the user at the button it has just switched off.
pub(super) fn no_dialog_guidance() -> String {
    guidance::configure_guidance(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The version that added the dialog is the one the call is answered by, so a
    /// portal reporting it can be asked and a Choose button on it is live.
    #[test]
    fn a_portal_that_reports_the_configure_version_can_be_asked() {
        assert!(supports(CONFIGURE_VERSION));
    }

    /// A portal below that version has no dialog, and a session cannot change
    /// that by waiting: the button has to be off and the guidance has to be up
    /// front rather than a refusal arriving after a call that cannot succeed.
    #[test]
    fn a_portal_below_the_configure_version_has_no_dialog() {
        assert!(
            !supports(CONFIGURE_VERSION - 1),
            "the version before the call is one that has no dialog"
        );
    }

    /// A portal that publishes no version at all is read as version 0, and a
    /// version 0 predates the dialog as surely as a version 1 does.
    #[test]
    fn a_portal_that_publishes_no_version_has_no_dialog() {
        assert!(!supports(0), "no version is not a supported one");
    }

    /// The gate lets a session that has a dialog through untouched: it is there
    /// to turn away the ones that have none, so a supported session must reach
    /// the call the same way it always did.
    #[test]
    fn a_session_with_a_dialog_is_not_turned_away() {
        can_configure(true).expect("a portal that can open its dialog is let through");
    }

    /// The one that has no dialog is turned away before the call is made, and it
    /// is the guidance alone that says so — a refused call would append a
    /// transport reason to a session that never had anything to refuse. The line
    /// it is turned away with names no press, because the buttons that would make
    /// one are the ones that are off.
    #[test]
    fn a_session_without_a_dialog_is_turned_away_with_the_guidance_alone() {
        let refused = can_configure(false)
            .expect_err("a portal with no dialog has no call to answer")
            .to_string();
        assert_eq!(
            refused,
            guidance::configure_guidance(false),
            "the guidance is the whole answer: {refused}"
        );
        assert!(
            !refused.contains("Choose again") && !refused.contains("press Save again"),
            "the answer never points at a button the session has switched off: {refused}"
        );
    }

    /// The line a window renders under its own disabled buttons is the same one
    /// the guard hands back, so the two can never tell the user different stories
    /// about the same session.
    #[test]
    fn the_line_a_window_renders_is_the_line_the_guard_gives() {
        assert_eq!(
            no_dialog_guidance(),
            guidance::configure_guidance(false),
            "one line for one session"
        );
    }

    /// A call that never opened the desktop's dialog leaves the user with no
    /// trigger and no button to press that would help, so the line has to open
    /// with the guidance for this session. The reason follows it, because the
    /// diagnostics window is where that detail is read.
    #[test]
    fn a_configure_failure_leads_with_this_session_guidance() {
        let reason = "the desktop portal refused ConfigureShortcuts: Signature mismatch";
        let refused = configure_failed(&anyhow!("{reason}")).to_string();
        assert!(
            refused.starts_with(&guidance::configure_guidance(true)),
            "the guidance for this session is the whole first part: {refused}"
        );
        assert!(
            refused.ends_with(&format!(": {reason}")),
            "the reason is kept whole after the guidance: {refused}"
        );
    }
}
