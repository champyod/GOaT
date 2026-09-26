use anyhow::{Result, anyhow};
use std::sync::Mutex;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};

use super::binding;
use super::dbus::ShortcutList;
use super::{Action, lock};

/// The single remap a `ShortcutsChanged` can be waiting for. A portal session is
/// reconfigured one dialog at a time, so there is only ever one answer to hand
/// over and arming a second wait takes the place of the first.
pub(super) struct Armed(Mutex<Option<Sender<ShortcutList>>>);

impl Armed {
    pub(super) fn new() -> Self {
        Self(Mutex::new(None))
    }

    pub(super) fn arm(&self) -> Receiver<ShortcutList> {
        let (tx, rx) = mpsc::channel();
        // Dropping the sender left here is what ends the previous wait, and it is
        // what makes that wait report a superseded request instead of a deadline.
        *lock(&self.0) = Some(tx);
        rx
    }

    /// Releases the wait when the call that opened the dialog failed, so no list
    /// is ever published to a remap that has already returned.
    pub(super) fn disarm(&self) {
        lock(&self.0).take();
    }

    /// A list the portal publishes is authoritative whoever asked for it: a
    /// change the user makes in their desktop's own settings arrives with no remap
    /// waiting for it, and the status line would then keep describing a trigger
    /// that is no longer bound. So a list that no longer has a reader is handed to
    /// `unclaimed`, which rebuilds that line. Kept apart from the session so both
    /// arrivals can be exercised without a bus.
    pub(super) fn route(
        &self,
        shortcuts: ShortcutList,
        unclaimed: impl FnOnce(&ShortcutList),
    ) -> Result<()> {
        match lock(&self.0).take() {
            Some(reader) => reader.send(shortcuts).map_err(|unread| {
                unclaimed(&unread.0);
                anyhow!("the shortcut list could not reach the remap waiting for it")
            }),
            None => {
                unclaimed(&shortcuts);
                Ok(())
            }
        }
    }
}

/// A wait that ran out of time and a wait whose place was taken by a newer remap
/// are different events, and only the first is the user failing to choose. A
/// superseded request is named as such, because the user is still looking at the
/// dialog the newer request opened.
pub(super) fn failed(outcome: RecvTimeoutError, action: Action) -> anyhow::Error {
    let id = binding::id_for(action);
    match outcome {
        RecvTimeoutError::Timeout => {
            anyhow!("no trigger was chosen for \"{id}\" within two minutes")
        }
        RecvTimeoutError::Disconnected => {
            anyhow!("the request to choose a trigger for \"{id}\" was replaced by a newer one")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hotkey::dbus::{self, PORTAL_PATH, SHORTCUTS_INTERFACE};
    use crate::hotkey::portal::CONFIGURE_TIMEOUT;
    use crate::hotkey::status::HotkeyStatus;
    use std::sync::Arc;
    use std::time::Duration;
    use zbus::Message;

    const TRIGGER: &str = "Ctrl+Shift+S";
    const TIMED_OUT: &str = "no trigger was chosen for \"goat_capture\" within two minutes";
    const SUPERSEDED: &str =
        "the request to choose a trigger for \"goat_capture\" was replaced by a newer one";

    /// Every status line the app would have published, in order. The recorder
    /// evaluates the same `portal_status` the app does, so what a test reads here
    /// is the line the window would have shown.
    type Read = Arc<Mutex<Vec<HotkeyStatus>>>;

    /// The capture shortcut as the portal publishes it, on a `ShortcutsChanged`
    /// signal, so a test's list travels the way a real one does. An empty trigger
    /// is the shape a shortcut the user released takes.
    fn list(trigger: &str) -> ShortcutList {
        let shortcuts = vec![(
            binding::CAPTURE_ID.to_string(),
            dbus::options(&[("trigger_description", trigger)]),
        )];
        let signal = Message::signal(PORTAL_PATH, SHORTCUTS_INTERFACE, "ShortcutsChanged")
            .and_then(|builder| builder.build(&shortcuts))
            .expect("the test signal serialises");
        signal
            .body()
            .deserialize()
            .expect("the test signal decodes")
    }

    fn recorder() -> (Read, impl Fn(&ShortcutList)) {
        let read: Read = Arc::new(Mutex::new(Vec::new()));
        let logged = Arc::clone(&read);
        let record = move |list: &ShortcutList| {
            let status = binding::portal_status(list);
            logged
                .lock()
                .expect("the test lock is not poisoned")
                .push(status);
        };
        (read, record)
    }

    fn read_from(read: &Read) -> Vec<HotkeyStatus> {
        read.lock().expect("the test lock is not poisoned").clone()
    }

    /// A change the user makes in their desktop's own settings reaches the app
    /// with no remap waiting for it. Leaving the line alone is how a released
    /// trigger keeps being reported as bound and a warning that no longer holds
    /// is never withdrawn, so both halves of the line are rebuilt from the list
    /// that arrived.
    #[test]
    fn a_change_in_the_desktop_settings_replaces_the_stale_status_line() {
        let armed = Armed::new();
        let (read, record) = recorder();
        armed
            .route(list(TRIGGER), &record)
            .expect("a list with no reader is not a failure");
        armed
            .route(list(""), &record)
            .expect("a second list with no reader is not a failure either");

        let published = read_from(&read);
        assert_eq!(published.len(), 2, "every list rebuilds the line");
        let (before, after) = (&published[0], &published[1]);
        assert!(before.detail.contains(TRIGGER), "a bound trigger is named");
        assert!(
            !after.detail.contains(TRIGGER),
            "the released trigger is gone"
        );
        assert!(
            after.warning.contains(binding::CAPTURE_ID),
            "the warning names the shortcut that lost its trigger: {}",
            after.warning
        );
    }

    /// The remap that asked for a list is the one that reads it, and it rebuilds
    /// the status line itself once the reconfigure is accepted.
    #[test]
    fn a_list_a_remap_asked_for_is_handed_to_that_remap() {
        let armed = Armed::new();
        let reader = armed.arm();
        let (read, record) = recorder();
        armed
            .route(list(TRIGGER), record)
            .expect("the reader is still waiting for it");
        assert!(
            read_from(&read).is_empty(),
            "a list with a reader is not read a second time"
        );
        let delivered = reader
            .recv_timeout(Duration::ZERO)
            .expect("the list reached the remap that armed the wait");
        assert_eq!(delivered.len(), 1, "the remap reads the list it asked for");
    }

    /// A reader that gave up first takes the list with it, which is a real failure
    /// to report, and the status line still owns a list nobody else will read.
    #[test]
    fn a_list_whose_reader_is_gone_is_reported_and_still_read() {
        let armed = Armed::new();
        armed.disarm();
        drop(armed.arm());
        let (read, record) = recorder();
        let refused = armed
            .route(list(TRIGGER), record)
            .expect_err("a list with no reader left is not delivered");
        assert!(
            refused
                .to_string()
                .contains("could not reach the remap waiting for it"),
            "the failure is reported, not discarded: {refused}"
        );
        assert_eq!(
            read_from(&read).len(),
            1,
            "the line is rebuilt from a list nobody was left to read"
        );
    }

    /// Arming a second wait drops the first sender, which ends that wait at once.
    /// The user is still answering the newer dialog, so the message has to say
    /// the request was replaced and must not accuse the desktop of silence.
    #[test]
    fn a_superseded_wait_is_reported_apart_from_a_deadline() {
        let armed = Armed::new();
        let superseded = armed.arm();
        let _current = armed.arm();
        let outcome = superseded
            .recv_timeout(CONFIGURE_TIMEOUT)
            .expect_err("the replaced wait ends as soon as its sender is dropped");
        assert!(
            matches!(outcome, RecvTimeoutError::Disconnected),
            "a replaced wait is not a wait that ran out of time: {outcome:?}"
        );
        assert_eq!(
            failed(outcome, Action::Capture).to_string(),
            SUPERSEDED,
            "a wait that was replaced says so"
        );
        assert_ne!(SUPERSEDED, TIMED_OUT, "the two outcomes read differently");
    }

    /// A wait nothing ever answers is the deadline itself, and it is the only
    /// outcome that may tell the user their desktop chose nothing.
    #[test]
    fn an_unanswered_wait_reports_the_deadline() {
        let armed = Armed::new();
        let waiting = armed.arm();
        // A zero deadline fails the way the real one does, so the mapping is
        // exercised against the error the channel itself produces.
        let outcome = waiting
            .recv_timeout(Duration::ZERO)
            .expect_err("nothing was ever sent, so only the deadline can end this wait");
        assert!(matches!(outcome, RecvTimeoutError::Timeout));
        assert_eq!(
            failed(outcome, Action::Capture).to_string(),
            TIMED_OUT,
            "a wait the user let run out says that"
        );
    }
}
