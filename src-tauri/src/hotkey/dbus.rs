use anyhow::{Result, anyhow};
use serde::Deserialize;
use std::collections::HashMap;
use std::ops::Deref;
use zbus::zvariant::{OwnedObjectPath, OwnedValue, Signature, Type, Value};
use zbus::{Connection, MatchRule, Message, MessageStream, Proxy};

pub const PORTAL_BUS: &str = "org.freedesktop.portal.Desktop";
pub const PORTAL_PATH: &str = "/org/freedesktop/portal/desktop";
pub const SHORTCUTS_INTERFACE: &str = "org.freedesktop.portal.GlobalShortcuts";

/// The portal can only parent a dialog to a window it can name, which on X11
/// means a window id. A Wayland surface handle needs xdg-foreign, which this
/// app never negotiates, and the spec reads an empty string as "no parent".
pub const NO_PARENT_WINDOW: &str = "";

/// One `v` of an `a{sv}`. Replies are read out of a bus buffer that does not
/// outlive the call, so each value is copied as it is decoded and the decoded
/// result stays valid after the message is gone.
#[derive(Clone, Debug)]
pub struct Variant(OwnedValue);

impl Type for Variant {
    const SIGNATURE: &'static Signature = &Signature::Variant;
}

impl<'de> Deserialize<'de> for Variant {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        OwnedValue::deserialize(deserializer).map(Variant)
    }
}

impl Deref for Variant {
    type Target = Value<'static>;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl Variant {
    pub fn text(&self) -> Result<String> {
        Value::clone(self)
            .downcast::<String>()
            .map_err(|e| anyhow!("expected text in the portal reply: {e}"))
    }

    pub fn shortcuts(&self) -> Result<ShortcutList> {
        let Value::Array(entries) = Value::clone(self) else {
            return Err(anyhow!("the portal reply is not a shortcut list"));
        };
        entries.iter().map(shortcut_of).collect()
    }
}

/// `a{sv}` on the way out.
pub type Options = HashMap<String, Value<'static>>;
/// `a{sv}` on the way back.
pub type Reply = HashMap<String, Variant>;
/// `a(sa{sv})` on the way back.
pub type ShortcutList = Vec<(String, Reply)>;

pub fn options(entries: &[(&str, &str)]) -> Options {
    entries
        .iter()
        .map(|(key, value)| ((*key).to_string(), Value::new((*value).to_string())))
        .collect()
}

pub async fn shortcuts_proxy(conn: &Connection) -> Result<Proxy<'static>> {
    Proxy::new(conn, PORTAL_BUS, PORTAL_PATH, SHORTCUTS_INTERFACE)
        .await
        .map_err(|e| anyhow!("{SHORTCUTS_INTERFACE} is not available on this bus: {e}"))
}

pub fn decode_shortcuts_changed(message: &Message) -> Result<(OwnedObjectPath, ShortcutList)> {
    message
        .body()
        .deserialize()
        .map_err(|e| anyhow!("cannot decode ShortcutsChanged: {e}"))
}

pub fn decode_activated(message: &Message) -> Result<(OwnedObjectPath, String)> {
    let decoded: (OwnedObjectPath, String, u64, Reply) = message
        .body()
        .deserialize()
        .map_err(|e| anyhow!("cannot decode Activated: {e}"))?;
    Ok((decoded.0, decoded.1))
}

pub fn member_of(message: &Message) -> String {
    message
        .header()
        .member()
        .map(|m| m.as_str().to_string())
        .unwrap_or_default()
}

/// Watches the portal's own signals for as long as the connection lives.
///
/// The sender is part of the rule because a signal's interface and member are
/// chosen by whoever emits it: any peer on the session bus can put
/// `org.freedesktop.portal.GlobalShortcuts` on a signal of its own and be read
/// as the portal. A match rule on a well-known name follows whoever owns that
/// name, so it admits the portal and nobody else. zbus removes the whole rule
/// again when the stream is dropped.
pub async fn subscribe(conn: &Connection, interface: &str) -> Result<MessageStream> {
    MessageStream::for_match_rule(portal_rule(interface)?, conn, Some(64))
        .await
        .map_err(|e| anyhow!("cannot subscribe to {interface} signals: {e}"))
}

fn portal_rule(interface: &str) -> Result<MatchRule<'_>> {
    let rule = MatchRule::builder()
        .msg_type(zbus::message::Type::Signal)
        .sender(PORTAL_BUS)
        .map_err(|e| anyhow!("{PORTAL_BUS} is not a usable bus name: {e}"))?
        .interface(interface)
        .map_err(|e| anyhow!("{interface} is not a usable interface name: {e}"))?
        .build();
    Ok(rule)
}

/// `a(sa{sv})`: the portal reports each shortcut as a pair of an id and its
/// properties. A variant cannot be turned straight into a nested Rust value, so
/// the array is walked one entry at a time.
fn shortcut_of(entry: &Value<'_>) -> Result<(String, Reply)> {
    let Value::Structure(fields) = entry else {
        return Err(anyhow!("a portal shortcut entry is not a pair"));
    };
    let mut parts = fields.fields().iter();
    let id = parts
        .next()
        .ok_or_else(|| anyhow!("a portal shortcut entry carries no id"))?
        .clone()
        .downcast::<String>()
        .map_err(|e| anyhow!("a portal shortcut id is not text: {e}"))?;
    let props = parts
        .next()
        .ok_or_else(|| anyhow!("a portal shortcut entry carries no properties"))?;
    Ok((id, properties_of(props)?))
}

fn properties_of(value: &Value<'_>) -> Result<Reply> {
    let Value::Dict(entries) = value else {
        return Err(anyhow!(
            "the properties of a portal shortcut are not a dictionary"
        ));
    };
    entries
        .iter()
        .map(|(key, value)| {
            let name = key
                .clone()
                .downcast::<String>()
                .map_err(|e| anyhow!("a portal option name is not text: {e}"))?;
            let owned = value
                .try_to_owned()
                .map_err(|e| anyhow!("cannot copy the portal option \"{name}\": {e}"))?;
            Ok((name, Variant(owned)))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The rule the watcher is installed with. A peer that emits the portal's
    /// interface on its own has to fall outside it, and the only thing that
    /// says so is the sender.
    #[test]
    fn the_watch_is_limited_to_the_portal_that_owns_the_name() {
        let rule = portal_rule(SHORTCUTS_INTERFACE).expect("the portal rule is usable");
        assert_eq!(
            rule.sender().map(|name| name.as_str()),
            Some(PORTAL_BUS),
            "only the process owning {PORTAL_BUS} is read as the portal"
        );
        assert_eq!(
            rule.interface().map(|name| name.as_str()),
            Some(SHORTCUTS_INTERFACE)
        );
        assert_eq!(rule.msg_type(), Some(zbus::message::Type::Signal));
        assert!(
            rule.path_spec().is_none() && rule.destination().is_none(),
            "the portal publishes these on its own object, not on one of ours"
        );
    }
}
