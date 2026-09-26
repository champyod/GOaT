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

/// Watches every signal of `interface` for as long as the connection lives.
pub async fn subscribe(conn: &Connection, interface: &str) -> Result<MessageStream> {
    let rule = MatchRule::builder()
        .msg_type(zbus::message::Type::Signal)
        .interface(interface)
        .map_err(|e| anyhow!("{interface} is not a usable interface name: {e}"))?
        .build();
    MessageStream::for_match_rule(rule, conn, Some(64))
        .await
        .map_err(|e| anyhow!("cannot subscribe to {interface} signals: {e}"))
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
