use std::sync::Arc;
use tauri::State;
use crate::AppState;

#[tauri::command]
pub async fn create_pty(
    cwd: String,
    cols: u16,
    rows: u16,
    app: tauri::AppHandle,
    state: State<'_, Arc<AppState>>,
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
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    use std::io::Write;
    let data_bytes = data.into_bytes();
    let instance = {
        let manager = state.pty_manager.lock()
            .map_err(|e| format!("Lock error: {}", e))?;
        manager.get_instance(&pty_id)
            .ok_or_else(|| format!("PTY not found: {}", pty_id))?
            .clone()
    };

    let mut guard = instance.lock()
        .map_err(|_| format!("Lock error on PTY instance"))?;
    guard.writer.write_all(&data_bytes)
        .map_err(|e| format!("Failed to write to PTY: {}", e))
}

#[tauri::command]
pub async fn resize_pty(
    pty_id: String,
    cols: u16,
    rows: u16,
    state: State<'_, Arc<AppState>>,
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
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let manager = &state.pty_manager;
    manager
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .destroy(&pty_id)
        .map_err(|e| format!("Failed to destroy PTY: {}", e))
}
