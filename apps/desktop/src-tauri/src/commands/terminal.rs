use tauri::State;
use crate::AppState;

#[tauri::command]
pub async fn create_pty(
    cwd: String,
    cols: u16,
    rows: u16,
    app: tauri::AppHandle,
    state: State<'_, AppState>,
) -> Result<String, String> {
    let manager = &state.pty_manager;
    manager
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .create(&cwd, cols, rows, Some(app))
        .map_err(|e| format!("Failed to create PTY: {}", e))
}

#[tauri::command]
pub async fn write_pty(
    pty_id: String,
    data: String,
    state: State<'_, AppState>,
) -> Result<(), String> {
    let manager = &state.pty_manager;
    manager
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .write(&pty_id, data.as_bytes())
        .map_err(|e| format!("Failed to write to PTY: {}", e))
}

#[tauri::command]
pub async fn resize_pty(
    pty_id: String,
    cols: u16,
    rows: u16,
    state: State<'_, AppState>,
) -> Result<(), String> {
    let manager = &state.pty_manager;
    manager
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .resize(&pty_id, cols, rows)
        .map_err(|e| format!("Failed to resize PTY: {}", e))
}

#[tauri::command]
pub async fn destroy_pty(
    pty_id: String,
    state: State<'_, AppState>,
) -> Result<(), String> {
    let manager = &state.pty_manager;
    manager
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .destroy(&pty_id)
        .map_err(|e| format!("Failed to destroy PTY: {}", e))
}
