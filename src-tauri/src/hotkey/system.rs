use anyhow::{Result, anyhow};
use tauri::{AppHandle, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};

use super::{Action, dispatch, lock, warn};
use crate::AppState;

/// Registers both triggers with the window system, which is the backend that
/// can see a key press on macOS, Windows and X11.
pub fn setup(app: &AppHandle, capture: &str, select: &str) -> tauri::Result<()> {
    let plugin = tauri_plugin_global_shortcut::Builder::new()
        .with_handler(|app, pressed, event| {
            if event.state == ShortcutState::Pressed
                && let Some(action) = action_for(app, *pressed)
            {
                dispatch(app, action);
            }
        })
        .build();
    app.plugin(plugin)?;
    let capture = parse(capture, "the primary")?;
    app.global_shortcut().register(capture).map_err(|e| {
        tauri::Error::Anyhow(anyhow!(
            "the window system rejected the capture hotkey: {e:#}"
        ))
    })?;
    let select = parse(select, "the region")?;
    // A region hotkey can collide with a user remap, so it is worth having but
    // not worth refusing to start over.
    if let Err(e) = app.global_shortcut().register(select) {
        warn(app, &format!("the region hotkey is not registered: {e}"));
    }
    Ok(())
}

pub fn remap(app: &AppHandle, previous: &str, next: &str) -> Result<()> {
    let replacement = parse(next, "the requested")?;
    let released = parse(previous, "the saved").ok();
    if let Some(shortcut) = released {
        // Releasing a trigger that was never taken reports an error. The handler
        // routes on the live state, so the remap still works, but the user is
        // told instead of the failure disappearing.
        if let Err(e) = app.global_shortcut().unregister(shortcut) {
            warn(
                app,
                &format!("the previous trigger \"{previous}\" was not released: {e}"),
            );
        }
    }
    match app.global_shortcut().register(replacement) {
        Ok(()) => Ok(()),
        Err(e) => Err(rollback(app, released, previous, next, e.into())),
    }
}

/// Puts the previous trigger back after a rejected remap, so one unusable key
/// does not leave the user with no hotkey at all.
fn rollback(
    app: &AppHandle,
    released: Option<Shortcut>,
    previous: &str,
    next: &str,
    rejected: anyhow::Error,
) -> anyhow::Error {
    let Some(shortcut) = released else {
        return anyhow!("the window system rejected \"{next}\": {rejected:#}");
    };
    match app.global_shortcut().register(shortcut) {
        Ok(()) => anyhow!("the window system rejected \"{next}\": {rejected:#}"),
        Err(e) => anyhow!(
            "\"{next}\" was rejected ({rejected}) and \"{previous}\" could not be put back: {e}"
        ),
    }
}

/// The window system reports only the key and the modifiers, so a press is
/// matched against whatever the app currently believes is bound.
fn action_for(app: &AppHandle, pressed: Shortcut) -> Option<Action> {
    let state = app.state::<AppState>();
    [Action::Capture, Action::ScreenSelect]
        .into_iter()
        .find(|action| {
            let saved = match action {
                Action::Capture => lock(&state.hotkey).clone(),
                Action::ScreenSelect => lock(&state.select_hotkey).clone(),
            };
            super::parse_shortcut(&saved)
                .is_some_and(|bound| bound.key == pressed.key && bound.mods == pressed.mods)
        })
}

fn parse(text: &str, which: &str) -> Result<Shortcut> {
    super::parse_shortcut(text)
        .ok_or_else(|| anyhow!("{which} hotkey \"{text}\" is not a valid shortcut"))
}
