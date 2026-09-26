use anyhow::{Result, anyhow};
use futures_util::StreamExt;
use std::time::{SystemTime, UNIX_EPOCH};
use zbus::zvariant::{OwnedObjectPath, Type};
use zbus::{Connection, MatchRule, MessageStream};

use super::dbus::{
    NO_PARENT_WINDOW, Options, Reply, ShortcutList, member_of, options, shortcuts_proxy,
};

const REQUEST_INTERFACE: &str = "org.freedesktop.portal.Request";
const REQUEST_PREFIX: &str = "/org/freedesktop/portal/desktop/request";
const HANDLE_TOKEN: &str = "handle_token";
const SESSION_HANDLE_TOKEN: &str = "session_handle_token";
const RESPONSE_MEMBER: &str = "Response";
const RESPONSE_SUCCESS: u32 = 0;

/// The object path the portal will publish a Request on, together with the
/// token it derives that path from. Predicting the path up front is what allows
/// the reply to be subscribed to before the call that triggers it.
struct RequestTarget {
    path: String,
    token: String,
}

/// Every portal method answers with a Request object path and delivers the real
/// result on a signal of that Request. `body` is handed the `handle_token` so
/// the caller can put it in its options dictionary, which is what makes the
/// Request path predictable — the subscription is then armed before the call,
/// so a portal that replies in the same turn cannot outrun the reader.
pub async fn call_request<B, F>(conn: &Connection, method: &str, body: F) -> Result<Reply>
where
    B: serde::Serialize + Type,
    F: FnOnce(&str) -> B,
{
    let target = request_target(conn)?;
    let mut signals = watch_request(conn, &target.path).await?;
    let handle: OwnedObjectPath = shortcuts_proxy(conn)
        .await?
        .call(method, &body(&target.token))
        .await
        .map_err(|e| anyhow!("the desktop portal refused {method}: {e}"))?;
    if handle.as_str() != target.path {
        return Err(anyhow!(
            "the portal answered {method} on {handle} instead of the requested {}",
            target.path
        ));
    }
    read_response(&mut signals, method).await
}

pub async fn create_session(conn: &Connection) -> Result<OwnedObjectPath> {
    let reply = call_request(conn, "CreateSession", |t| {
        // `session_handle_token` is optional in the spec, but the session object
        // path is derived from it and xdg-desktop-portal aborts the whole
        // process when it is missing, so it is always supplied.
        (options(&[(HANDLE_TOKEN, t), (SESSION_HANDLE_TOKEN, t)]),)
    })
    .await?;
    let handle = reply
        .get("session_handle")
        .ok_or_else(|| anyhow!("the portal created a session without a handle"))?
        .text()?;
    OwnedObjectPath::try_from(handle.as_str())
        .map_err(|e| anyhow!("the portal returned an unusable session path \"{handle}\": {e}"))
}

pub async fn bind_shortcuts(
    conn: &Connection,
    session: &OwnedObjectPath,
    shortcuts: Vec<(String, Options)>,
) -> Result<ShortcutList> {
    let reply = call_request(conn, "BindShortcuts", |t| {
        (
            session.clone(),
            shortcuts,
            NO_PARENT_WINDOW,
            options(&[(HANDLE_TOKEN, t)]),
        )
    })
    .await?;
    reply
        .get("shortcuts")
        .ok_or_else(|| anyhow!("the reply to BindShortcuts carries no shortcut list"))?
        .shortcuts()
}

pub async fn configure_shortcuts(conn: &Connection, session: &OwnedObjectPath) -> Result<()> {
    call_request(conn, "ConfigureShortcuts", |t| {
        (
            session.clone(),
            NO_PARENT_WINDOW,
            options(&[(HANDLE_TOKEN, t)]),
        )
    })
    .await
    .map(|_| ())
}

/// The portal builds a Request object path as `/…/request/SENDER/TOKEN`, where
/// SENDER is the caller's unique name with the leading colon dropped and every
/// dot turned into an underscore, and TOKEN is the caller's `handle_token`. Both
/// have to be valid path elements, which is why the sender is rewritten rather
/// than copied.
fn request_target(conn: &Connection) -> Result<RequestTarget> {
    let sender = conn
        .unique_name()
        .ok_or_else(|| anyhow!("the bus gave this process no unique name"))?
        .as_str();
    let sender: String = sender.strip_prefix(':').unwrap_or(sender).replace('.', "_");
    let token = next_token();
    Ok(RequestTarget {
        path: format!("{REQUEST_PREFIX}/{sender}/{token}"),
        token,
    })
}

/// The portal asks for a per-library prefix and a number that is not guessable,
/// and requires the result to match [A-Za-z0-9_]+.
fn next_token() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or_default();
    format!("goat{}_{nanos:x}", std::process::id())
}

async fn watch_request(conn: &Connection, path: &str) -> Result<MessageStream> {
    let rule = MatchRule::builder()
        .msg_type(zbus::message::Type::Signal)
        .interface(REQUEST_INTERFACE)
        .map_err(|e| anyhow!("{REQUEST_INTERFACE} is not a usable interface name: {e}"))?
        .path(path)
        .map_err(|e| anyhow!("{path} is not a usable object path: {e}"))?
        .build();
    MessageStream::for_match_rule(rule, conn, Some(8))
        .await
        .map_err(|e| anyhow!("cannot watch the portal request at {path}: {e}"))
}

async fn read_response(signals: &mut MessageStream, method: &str) -> Result<Reply> {
    let message = signals
        .next()
        .await
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
