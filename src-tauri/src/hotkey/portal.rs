use anyhow::{Result, anyhow};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Manager};
use zbus::Connection;
use zbus::zvariant::OwnedObjectPath;

use super::binding;
use super::dbus::{self, ShortcutList};
use super::request;
use super::signal;
use super::{Action, dispatch, lock, warn};

/// How a signal the app could not read reaches the user. The watcher outlives
/// the bad message, so this is a warning about one message rather than the end
/// of the hotkeys, and it says so.
const IGNORED_SIGNAL: &str = "a desktop shortcut signal was ignored";

/// A configure dialog the user walks away from would otherwise leave the remap
/// command pending forever, so the wait for its answer is bounded. It bounds
/// that answer and nothing else: the reply to the call that opened the dialog
/// is a different signal and is bounded by `RESPONSE_TIMEOUT`.
const CONFIGURE_TIMEOUT: Duration = Duration::from_secs(120);

/// A portal that accepts a call and then says nothing has bound nothing, and
/// the app cannot tell that from a portal that worked — the status line would
/// keep reporting a live backend and no warning would be raised. Bounding the
/// reply turns it into the error startup already reports.
pub(super) const RESPONSE_TIMEOUT: Duration = Duration::from_secs(30);

/// What the portal reported for one binding round, as shown to the user.
pub struct BindingReport {
    pub detail: String,
    pub warning: String,
}

pub struct Portal {
    conn: Connection,
    session: OwnedObjectPath,
    waiting: Arc<Mutex<Option<Sender<ShortcutList>>>>,
}

impl Clone for Portal {
    fn clone(&self) -> Self {
        Self {
            conn: self.conn.clone(),
            session: self.session.clone(),
            waiting: Arc::clone(&self.waiting),
        }
    }
}

/// Creates the portal session, binds both shortcuts exactly once, and leaves a
/// task watching for activations. The session is only published to the app once
/// the bind succeeded, and a session may never bind twice, so a later remap can
/// only reconfigure it.
pub async fn start(app: &AppHandle) -> Result<(Portal, BindingReport)> {
    let conn = Connection::session()
        .await
        .map_err(|e| anyhow!("cannot reach the session bus: {e}"))?;
    let session = request::create_session(&conn).await?;
    let portal = Portal {
        conn: conn.clone(),
        session,
        waiting: Arc::new(Mutex::new(None)),
    };
    let bound = request::bind_shortcuts(&conn, &portal.session, binding::requested()).await?;
    // A session may only bind once, so the report is computed from this reply
    // and never recomputed by a second bind.
    let report = BindingReport {
        detail: binding::describe(&bound),
        warning: binding::binding_warning(&bound),
    };
    watch(portal.clone(), app.clone());
    app.manage(portal.clone());
    Ok((portal, report))
}

/// A remap cannot bind a second time, so it asks the portal to reconfigure the
/// existing session and reads the outcome from `ShortcutsChanged`.
pub async fn remap(app: &AppHandle, action: Action) -> Result<ShortcutList> {
    let portal = app.try_state::<Portal>().ok_or_else(|| {
        anyhow!("the desktop shortcut portal is not ready yet; try again in a moment")
    })?;
    let answer = arm(&portal);
    if let Err(e) = portal.configure().await {
        disarm(&portal);
        return Err(e);
    }
    let chosen = waited(answer, action).await?;
    binding::require_trigger(&chosen, action)?;
    Ok(chosen)
}

async fn waited(answer: Receiver<ShortcutList>, action: Action) -> Result<ShortcutList> {
    let deadline =
        tauri::async_runtime::spawn_blocking(move || answer.recv_timeout(CONFIGURE_TIMEOUT));
    deadline
        .await
        .map_err(|e| anyhow!("cannot wait for the shortcut dialog: {e}"))?
        .map_err(|_| {
            anyhow!(
                "no trigger was chosen for \"{}\" within two minutes",
                binding::id_for(action)
            )
        })
}

fn arm(portal: &Portal) -> Receiver<ShortcutList> {
    let (tx, rx) = mpsc::channel();
    *lock(&portal.waiting) = Some(tx);
    rx
}

fn disarm(portal: &Portal) {
    lock(&portal.waiting).take();
}

fn watch(portal: Portal, app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        if let Err(e) = portal.receive_signals(&app).await {
            warn(
                &app,
                &format!("the desktop shortcut portal stopped responding: {e}"),
            );
        }
    });
}

impl Portal {
    async fn configure(&self) -> Result<()> {
        request::configure_shortcuts(&self.conn, &self.session).await
    }

    async fn receive_signals(&self, app: &AppHandle) -> Result<()> {
        let mut signals = dbus::subscribe(&self.conn, dbus::SHORTCUTS_INTERFACE).await?;
        signal::watch(
            &mut signals,
            |message| self.handle(app, message),
            |e| warn(app, &format!("{IGNORED_SIGNAL}: {e}")),
        )
        .await
    }

    fn handle(&self, app: &AppHandle, message: &zbus::Message) -> Result<()> {
        match signal::classify(message)? {
            signal::Signal::Activated { session, id } if session == self.session => {
                if let Some(action) = binding::action_for(&id) {
                    dispatch(app, action);
                }
                Ok(())
            }
            signal::Signal::ShortcutsChanged { session, shortcuts } if session == self.session => {
                self.deliver(shortcuts)
            }
            _ => Ok(()),
        }
    }

    /// Hands a reconfigured list to the remap waiting on it. A list that arrives
    /// with no remap waiting is the portal confirming a bind from startup, and
    /// the reply that already reported it needs no second reading.
    fn deliver(&self, shortcuts: ShortcutList) -> Result<()> {
        if let Some(waiting) = lock(&self.waiting).take() {
            let _ = waiting.send(shortcuts);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The two deadlines guard different waits, so neither may stand in for the
    /// other: the one on the reply has to be short enough to fail a startup that
    /// will never succeed, while the one on the dialog has to outlast a user
    /// walking to another window to press a key.
    #[test]
    fn the_reply_deadline_is_shorter_than_the_dialog_deadline() {
        assert!(
            RESPONSE_TIMEOUT < CONFIGURE_TIMEOUT,
            "a portal that never replies must not hold a startup for {} seconds",
            CONFIGURE_TIMEOUT.as_secs()
        );
        assert!(
            RESPONSE_TIMEOUT >= Duration::from_secs(5),
            "a portal that opens a dialog in its own time still has to fit inside {}",
            RESPONSE_TIMEOUT.as_secs()
        );
    }
}
