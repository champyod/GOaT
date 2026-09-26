use anyhow::{Result, anyhow};
use futures_util::StreamExt;
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Manager};
use zbus::Connection;
use zbus::zvariant::OwnedObjectPath;

use super::binding;
use super::dbus::{self, ShortcutList};
use super::request;
use super::{Action, dispatch, lock, warn};

/// A configure dialog the user walks away from would otherwise leave the remap
/// command pending forever, so the wait for its answer is bounded.
const CONFIGURE_TIMEOUT: Duration = Duration::from_secs(120);

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
        while let Some(message) = signals.next().await {
            let message = message.map_err(|e| anyhow!("the portal signal stream failed: {e}"))?;
            self.handle(app, &message)?;
        }
        Err(anyhow!(
            "the desktop closed the global shortcuts connection"
        ))
    }

    fn handle(&self, app: &AppHandle, message: &zbus::Message) -> Result<()> {
        match dbus::member_of(message).as_str() {
            "Activated" => self.on_activated(app, message),
            "ShortcutsChanged" => self.on_changed(message),
            _ => Ok(()),
        }
    }

    fn on_activated(&self, app: &AppHandle, message: &zbus::Message) -> Result<()> {
        let (session, id) = dbus::decode_activated(message)?;
        if session != self.session {
            return Ok(());
        }
        if let Some(action) = binding::action_for(&id) {
            dispatch(app, action);
        }
        Ok(())
    }

    fn on_changed(&self, message: &zbus::Message) -> Result<()> {
        let (session, shortcuts) = dbus::decode_shortcuts_changed(message)?;
        if session != self.session {
            return Ok(());
        }
        if let Some(waiting) = lock(&self.waiting).take() {
            let _ = waiting.send(shortcuts);
        }
        Ok(())
    }
}
