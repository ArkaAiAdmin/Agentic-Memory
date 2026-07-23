use tauri::State;
use crate::AppState;

#[tauri::command]
pub async fn git_status(
    repo_path: String,
    state: State<'_, AppState>,
) -> Result<String, String> {
    let git = &state.git;
    git.lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .status(&repo_path)
        .map_err(|e| format!("Git status failed: {}", e))
}

#[tauri::command]
pub async fn git_diff(
    repo_path: String,
    file_path: Option<String>,
    state: State<'_, AppState>,
) -> Result<String, String> {
    let git = &state.git;
    git.lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .diff(&repo_path, file_path.as_deref())
        .map_err(|e| format!("Git diff failed: {}", e))
}

#[tauri::command]
pub async fn git_log(
    repo_path: String,
    limit: u32,
    state: State<'_, AppState>,
) -> Result<String, String> {
    let git = &state.git;
    git.lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .log(&repo_path, limit)
        .map_err(|e| format!("Git log failed: {}", e))
}

#[tauri::command]
pub async fn git_commit(
    repo_path: String,
    message: String,
    state: State<'_, AppState>,
) -> Result<(), String> {
    let git = &state.git;
    git.lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .commit(&repo_path, &message)
        .map_err(|e| format!("Git commit failed: {}", e))
}

#[tauri::command]
pub async fn git_branch(
    repo_path: String,
    state: State<'_, AppState>,
) -> Result<String, String> {
    let git = &state.git;
    git.lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .current_branch(&repo_path)
        .map_err(|e| format!("Git branch failed: {}", e))
}
