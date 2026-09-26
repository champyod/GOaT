use anyhow::{Result, anyhow};
use futures_util::{Stream, StreamExt};
use std::time::Duration;
use tokio::time::timeout;
use zbus::{Connection, MatchRule, Message, MessageStream};

use super::dbus::{Reply, member_of};

const REQUEST_INTERFACE: &str = "org.freedesktop.portal.Request";
const RESPONSE_MEMBER: &str = "Response";
const RESPONSE_SUCCESS: u32 = 0;
/// The subscription is armed before the call that triggers the reply, so a
/// portal that answers in the same turn cannot outrun the reader, and the queue
/// only has to hold what arrives in between.
const QUEUE: usize = 8;

/// Watches one Request object for the reply the portal will put on it.
pub async fn watch(conn: &Connection, path: &str) -> Result<MessageStream> {
    let rule = MatchRule::builder()
        .msg_type(zbus::message::Type::Signal)
        .interface(REQUEST_INTERFACE)
        .map_err(|e| anyhow!("{REQUEST_INTERFACE} is not a usable interface name: {e}"))?
        .path(path)
        .map_err(|e| anyhow!("{path} is not a usable object path: {e}"))?
        .build();
    MessageStream::for_match_rule(rule, conn, Some(QUEUE))
        .await
        .map_err(|e| anyhow!("cannot watch the portal request at {path}: {e}"))
}

/// Reads the reply, and fails when none arrives.
///
/// The bus carries no deadline of its own, and a stream that a quiet peer leaves
/// alone stays pending indefinitely, so without the bound below a portal that
/// accepted a call and then said nothing would hold the call open forever — at
/// startup with nothing bound and nothing to report, and on a remap with the
/// save button looking dead. The bound is what turns a silent portal into the
/// error the caller already knows how to report.
pub async fn read<S>(signals: &mut S, method: &str, within: Duration) -> Result<Reply>
where
    S: Stream<Item = Result<Message, zbus::Error>> + Unpin,
{
    let message = timeout(within, signals.next())
        .await
        .map_err(|_| {
            anyhow!(
                "the desktop portal never answered {method} within {} seconds",
                within.as_secs()
            )
        })?
        .ok_or_else(|| anyhow!("the portal closed the request for {method} without answering"))?
        .map_err(|e| anyhow!("the portal request stream failed: {e}"))?;
    if member_of(&message) != RESPONSE_MEMBER {
        return Err(anyhow!(
            "the {method} dialog was dismissed before it completed"
        ));
    }
    let (code, results): (u32, Reply) = message
        .body()
        .deserialize()
        .map_err(|e| anyhow!("cannot decode the reply to {method}: {e}"))?;
    if code != RESPONSE_SUCCESS {
        return Err(anyhow!(
            "the portal ended {method} with response code {code}"
        ));
    }
    Ok(results)
}

#[cfg(test)]
mod tests {
    use super::super::dbus;
    use super::*;
    use futures_util::stream;

    const BIND: &str = "BindShortcuts";
    const SESSION_HANDLE: &str = "/org/freedesktop/portal/desktop/session/goat/1";
    const REQUEST_PATH: &str = "/org/freedesktop/portal/desktop/request/goat/1";

    /// A portal that accepted the call and then stayed silent, which is the
    /// case the deadline exists for: this stream yields nothing and never will,
    /// so the only way out of the read is the deadline itself.
    fn silence() -> impl Stream<Item = Result<Message, zbus::Error>> + Unpin {
        stream::pending()
    }

    /// A reply on the Request the portal published, built the way the portal
    /// emits it so the decode under test is the real one.
    fn response(code: u32, results: dbus::Options) -> Message {
        Message::signal(REQUEST_PATH, REQUEST_INTERFACE, RESPONSE_MEMBER)
            .and_then(|builder| builder.build(&(code, results)))
            .expect("the test response serialises")
    }

    fn read_now<S>(signals: &mut S) -> Result<Reply>
    where
        S: Stream<Item = Result<Message, zbus::Error>> + Unpin,
    {
        tauri::async_runtime::block_on(read(signals, BIND, Duration::ZERO))
    }

    /// The startup false pass: with nothing on the wire the read has to come
    /// back as an error, or nothing was ever bound and the app says otherwise.
    #[test]
    fn a_portal_that_never_answers_fails_instead_of_waiting_forever() {
        let mut signals = silence();
        let refused = read_now(&mut signals).expect_err(
            "a portal that accepts a call and says nothing is a failure, not a success",
        );
        assert_eq!(
            refused.to_string(),
            "the desktop portal never answered BindShortcuts within 0 seconds"
        );
    }

    #[test]
    fn a_portal_that_ends_the_stream_without_responding_is_reported_as_closed() {
        let mut signals = stream::iter(Vec::new());
        let refused =
            read_now(&mut signals).expect_err("a reply that never comes is not a bound shortcut");
        assert_eq!(
            refused.to_string(),
            "the portal closed the request for BindShortcuts without answering"
        );
    }

    #[test]
    fn a_successful_response_is_read_through_the_deadline() {
        let mut signals = stream::iter(vec![Ok(response(
            RESPONSE_SUCCESS,
            dbus::options(&[("session_handle", SESSION_HANDLE)]),
        ))]);
        let reply = read_now(&mut signals).expect("a reply inside the deadline is accepted");
        assert_eq!(
            reply
                .get("session_handle")
                .map(|v| v.text())
                .transpose()
                .expect("the handle is text"),
            Some(SESSION_HANDLE.to_string())
        );
    }

    #[test]
    fn a_refused_response_is_still_an_error() {
        let mut signals = stream::iter(vec![Ok(response(1, dbus::options(&[])))]);
        let refused = read_now(&mut signals).expect_err("a non-zero response code is a refusal");
        assert_eq!(
            refused.to_string(),
            "the portal ended BindShortcuts with response code 1"
        );
    }

    /// A dialog the user walked away from shows up as a `Closed` signal on the
    /// Request, which is not the member this app is waiting for.
    #[test]
    fn a_dismissed_dialog_is_told_apart_from_a_silent_portal() {
        let dismissed = Message::signal(REQUEST_PATH, REQUEST_INTERFACE, "Closed")
            .and_then(|builder| builder.build(&()))
            .expect("the test signal serialises");
        let mut signals = stream::iter(vec![Ok(dismissed)]);
        let refused = read_now(&mut signals).expect_err("a dismissed dialog binds nothing");
        assert_eq!(
            refused.to_string(),
            "the BindShortcuts dialog was dismissed before it completed"
        );
    }
}
