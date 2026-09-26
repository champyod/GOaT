use tauri::AppHandle;

use super::report;

/// The window starts hidden, so anything the user has to act on is announced by
/// the desktop itself rather than waiting to be read inside the app.
pub fn startup_notice(app: &AppHandle, explanation: &str, warning: &str) {
    use tauri_plugin_notification::NotificationExt;
    let body = if warning.is_empty() {
        explanation.to_string()
    } else {
        format!("{explanation}\n\n{warning}")
    };
    if let Err(e) = app
        .notification()
        .builder()
        .title("GOaT needs a global hotkey")
        .body(body)
        .show()
    {
        report(app, &format!("the hotkey notice could not be shown: {e}"));
    }
}
