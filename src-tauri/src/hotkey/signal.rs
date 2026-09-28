use anyhow::{Result, anyhow};
use futures_util::{Stream, StreamExt};
use zbus::Message;
use zbus::zvariant::OwnedObjectPath;

use super::dbus::{self, ShortcutList};

const ACTIVATED: &str = "Activated";
const SHORTCUTS_CHANGED: &str = "ShortcutsChanged";

/// How a signal of the portal's interface reaches the one thing that acts on it.
#[derive(Debug)]
pub enum Signal {
    Activated {
        session: OwnedObjectPath,
        id: String,
    },
    ShortcutsChanged {
        session: OwnedObjectPath,
        shortcuts: ShortcutList,
    },
    /// A member of the same interface that this app has no use for. It arrives
    /// with a body that is never looked at, so it costs nothing to skip.
    Other,
}

/// Reads a signal out of its body. The decode is where a message the portal
/// changed the shape of turns into an error, and that error has to stay
/// survivable, so it is kept here rather than folded into the watch.
pub fn classify(message: &Message) -> Result<Signal> {
    match dbus::member_of(message).as_str() {
        ACTIVATED => {
            let (session, id) = dbus::decode_activated(message)?;
            Ok(Signal::Activated { session, id })
        }
        SHORTCUTS_CHANGED => {
            let (session, shortcuts) = dbus::decode_shortcuts_changed(message)?;
            Ok(Signal::ShortcutsChanged { session, shortcuts })
        }
        _ => Ok(Signal::Other),
    }
}

/// The watcher's body, kept apart from the bus so the failure policy can be
/// exercised without one. Nothing re-arms the watcher, so a message this app
/// cannot read is reported and skipped rather than allowed to end the loop,
/// which would leave every later hotkey dead with no user-visible sign. Only a
/// stream that has ended ends the watch.
pub async fn watch<S, H, W>(mut signals: S, mut handle: H, mut report: W) -> Result<()>
where
    S: Stream<Item = Result<Message, zbus::Error>> + Unpin,
    H: FnMut(&Message) -> Result<()>,
    W: FnMut(anyhow::Error),
{
    while let Some(delivered) = signals.next().await {
        let outcome = match delivered {
            Ok(message) => handle(&message),
            Err(e) => Err(anyhow!("the portal signal stream failed: {e}")),
        };
        if let Err(e) = outcome {
            report(e);
        }
    }
    Err(anyhow!(
        "the desktop closed the global shortcuts connection"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use futures_util::stream;
    use serde::Serialize;
    use std::sync::Mutex;
    use zbus::zvariant::DynamicType;

    const CAPTURE_ID: &str = "goat_capture";
    const SESSION: &str = "/org/freedesktop/portal/desktop/session/goat/1";
    const WRONG_BODY: &str = "a body of the wrong shape";

    /// Builds a signal the way the portal emits one, so the tests read and
    /// decode real message bodies rather than a stand-in for them.
    fn signal<B: Serialize + DynamicType>(member: &str, body: &B) -> Message {
        Message::signal(dbus::PORTAL_PATH, dbus::SHORTCUTS_INTERFACE, member)
            .and_then(|builder| builder.build(body))
            .expect("the test signal serialises")
    }

    fn session() -> OwnedObjectPath {
        OwnedObjectPath::try_from(SESSION).expect("the test path is valid")
    }

    fn activated(id: &str) -> Message {
        signal(
            ACTIVATED,
            &(session(), id.to_string(), 0_u64, dbus::options(&[])),
        )
    }

    /// What a peer speaking a later version of the interface looks like: the
    /// member is the one this app listens for, the body is not the one it reads.
    fn unreadable() -> Message {
        signal(ACTIVATED, &WRONG_BODY)
    }

    #[test]
    fn reads_the_shortcut_out_of_an_activation() {
        let Signal::Activated { session, id } =
            classify(&activated(CAPTURE_ID)).expect("a well-formed activation decodes")
        else {
            panic!("an activation is never another kind of signal");
        };
        assert_eq!(id, CAPTURE_ID);
        assert_eq!(session.as_str(), SESSION);
    }

    #[test]
    fn a_signal_of_another_member_is_passed_over() {
        let message = signal("Deactivated", &session());
        let classified = classify(&message);
        assert!(
            matches!(classified, Ok(Signal::Other)),
            "a member with no body of its own costs nothing to skip: {classified:?}"
        );
    }

    #[test]
    fn a_shortcut_list_decodes_into_the_signal_the_watch_acts_on() {
        let shortcuts: Vec<(String, dbus::Options)> = vec![(
            CAPTURE_ID.to_string(),
            dbus::options(&[("trigger_description", "Ctrl+Shift+S")]),
        )];
        let body = (session(), shortcuts);
        let Signal::ShortcutsChanged { session, shortcuts } =
            classify(&signal(SHORTCUTS_CHANGED, &body)).expect("a well-formed change decodes")
        else {
            panic!("a change is never another kind of signal");
        };
        assert_eq!(session.as_str(), SESSION);
        assert_eq!(
            shortcuts[0]
                .1
                .get("trigger_description")
                .expect("the trigger is in the list")
                .text()
                .expect("the trigger is text"),
            "Ctrl+Shift+S"
        );
    }

    /// The regression this loop exists for: the bad message has to be reported
    /// and the good one behind it still read, because a watcher that ends here
    /// is never re-armed.
    #[test]
    fn an_unreadable_signal_is_reported_and_the_next_one_still_read() {
        let read = Mutex::new(Vec::new());
        let reported = Mutex::new(Vec::new());
        let signals = stream::iter(vec![Ok(unreadable()), Ok(activated(CAPTURE_ID))]);
        let ended = tauri::async_runtime::block_on(watch(
            signals,
            |message| {
                let outcome = classify(message);
                let seen = match &outcome {
                    Ok(Signal::Activated { id, .. }) => format!("activated {id}"),
                    Ok(_) => "another member of the interface".to_string(),
                    Err(e) => format!("unreadable: {e}"),
                };
                read.lock()
                    .expect("the test lock is not poisoned")
                    .push(seen);
                outcome.map(|_| ())
            },
            |e| {
                reported
                    .lock()
                    .expect("the test lock is not poisoned")
                    .push(e.to_string());
            },
        ))
        .expect_err("a stream that ends ends the watch");

        assert!(
            ended
                .to_string()
                .contains("closed the global shortcuts connection"),
            "the watch only ends when the stream does: {ended}"
        );
        let read = read.lock().expect("the test lock is not poisoned");
        assert_eq!(read.len(), 2, "both messages reach the handler: {read:?}");
        assert!(
            read[0].starts_with("unreadable: cannot decode Activated"),
            "the first message is the one that could not be read: {}",
            read[0]
        );
        assert_eq!(
            read[1],
            format!("activated {CAPTURE_ID}"),
            "the activation behind it is still read"
        );
        let reported = reported.lock().expect("the test lock is not poisoned");
        assert_eq!(reported.len(), 1, "only the unreadable message is reported");
        assert!(
            reported[0].contains("cannot decode Activated"),
            "the report says the body could not be read: {}",
            reported[0]
        );
    }
}
