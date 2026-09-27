// A module declared inside a non-`mod.rs` file looks under `portal/`, and this
// one sits beside `portal.rs` with the rest of the hotkey backend.
#[path = "guidance.rs"]
mod guidance;

use anyhow::{Result, anyhow};
use std::sync::Arc;
use std::sync::mpsc::Receiver;
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager};
use zbus::Connection;
use zbus::zvariant::OwnedObjectPath;

use super::binding;
use super::dbus::{self, ShortcutList};
use super::request;
use super::signal;
use super::waiting::{self, Armed};
use super::{Action, dispatch, warn};

/// How a signal the app could not read reaches the user. The watcher outlives
/// the bad message, so this is a warning about one message rather than the end
/// of the hotkeys, and it says so.
const IGNORED_SIGNAL: &str = "a desktop shortcut signal was ignored";

/// A configure dialog the user walks away from would otherwise leave the remap
/// command pending forever, so the wait for its answer is bounded. It bounds
/// that answer and nothing else: the reply to the call that opened the dialog
/// is a different signal and is bounded by `RESPONSE_TIMEOUT`.
pub(super) const CONFIGURE_TIMEOUT: Duration = Duration::from_secs(120);

/// A portal that accepts a call and then says nothing has bound nothing, and
/// the app cannot tell that from a portal that worked — the status line would
/// keep reporting a live backend and no warning would be raised. Bounding the
/// reply turns it into the error startup already reports.
pub(super) const RESPONSE_TIMEOUT: Duration = Duration::from_secs(30);

/// Once the dialog is up the wait belongs to the desktop, but the app cannot tell
/// that from a call still in flight, so it would report the bind as pending for as
/// long as the user takes to answer. The dialog announces itself so the wait starts
/// where the user's attention does. A window that cannot hear it is not worth
/// failing a remap that has already succeeded.
pub(super) const DIALOG_OPENED: &str = "hotkey-dialog-opened";

/// What the portal reported for one binding round, as shown to the user. The two
/// triggers come out of the same reply as the line, because the keys the portal
/// will fire are the ones it reported and no stored shortcut can name them.
pub struct BindingReport {
    pub detail: String,
    pub warning: String,
    pub capture_trigger: Option<String>,
    pub select_trigger: Option<String>,
}

pub struct Portal {
    conn: Connection,
    session: OwnedObjectPath,
    armed: Arc<Armed>,
}

impl Clone for Portal {
    fn clone(&self) -> Self {
        Self {
            conn: self.conn.clone(),
            session: self.session.clone(),
            armed: Arc::clone(&self.armed),
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
        armed: Arc::new(Armed::new()),
    };
    let bound = request::bind_shortcuts(&conn, &portal.session, binding::requested()).await?;
    // A session may only bind once, so the report is computed from this reply
    // and never recomputed by a second bind.
    let report = BindingReport {
        detail: binding::describe(&bound),
        warning: binding::binding_warning(&bound),
        capture_trigger: binding::trigger_for(&bound, binding::CAPTURE_ID),
        select_trigger: binding::trigger_for(&bound, binding::SELECT_ID),
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
    let answer = portal.armed.arm();
    if let Err(e) = portal.configure().await {
        portal.armed.disarm();
        return Err(configure_failed(&e));
    }
    let _ = app.emit(DIALOG_OPENED, binding::id_for(action));
    let chosen = waited(answer, action).await?;
    binding::require_trigger(&chosen, action)?;
    Ok(chosen)
}

/// Puts the instruction in front of the reason, because the reason alone names a
/// transport failure the user cannot act on while the instruction is the only step
/// left. The reason is kept whole, so the diagnostics window still holds it.
fn configure_failed(reason: &anyhow::Error) -> anyhow::Error {
    anyhow!("{}: {reason:#}", guidance::configure_guidance())
}

async fn waited(answer: Receiver<ShortcutList>, action: Action) -> Result<ShortcutList> {
    let deadline =
        tauri::async_runtime::spawn_blocking(move || answer.recv_timeout(CONFIGURE_TIMEOUT));
    let chosen = deadline
        .await
        .map_err(|e| anyhow!("cannot wait for the shortcut dialog: {e}"))?;
    chosen.map_err(|outcome| waiting::failed(outcome, action))
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
                self.deliver(app, shortcuts)
            }
            _ => Ok(()),
        }
    }

    /// Hands a reconfigured list to the remap waiting on it, and reads a list
    /// nobody asked for into the status line.
    fn deliver(&self, app: &AppHandle, shortcuts: ShortcutList) -> Result<()> {
        self.armed.route(shortcuts, |unclaimed| {
            super::record_triggers(app, unclaimed)
        })
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

    /// A call that never opened the desktop's dialog leaves the user with no
    /// trigger and no button to press that would help, so the line has to open
    /// with the guidance for this session. The reason follows it, because the
    /// diagnostics window is where that detail is read.
    #[test]
    fn a_configure_failure_leads_with_this_session_guidance() {
        let reason = "the desktop portal refused ConfigureShortcuts: Signature mismatch";
        let refused = configure_failed(&anyhow!("{reason}")).to_string();
        assert!(
            refused.starts_with(&guidance::configure_guidance()),
            "the guidance for this session is the whole first part: {refused}"
        );
        assert!(
            refused.ends_with(&format!(": {reason}")),
            "the reason is kept whole after the guidance: {refused}"
        );
    }
}
