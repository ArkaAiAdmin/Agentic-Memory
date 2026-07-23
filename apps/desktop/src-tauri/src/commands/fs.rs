use serde::Serialize;
use std::path::PathBuf;
use tauri::State;
use crate::AppState;

#[tauri::command]
pub async fn read_file(path: String) -> Result<String, String> {
    tokio::fs::read_to_string(&path)
        .await
        .map_err(|e| format!("Failed to read file: {}", e))
}

#[tauri::command]
pub async fn write_file(path: String, content: String) -> Result<(), String> {
    // Ensure parent directory exists
    if let Some(parent) = PathBuf::from(&path).parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|e| format!("Failed to create directory: {}", e))?;
    }

    tokio::fs::write(&path, &content)
        .await
        .map_err(|e| format!("Failed to write file: {}", e))
}

#[derive(Serialize)]
pub struct DirEntry {
    pub name: String,
    pub is_dir: bool,
}

#[tauri::command]
pub async fn list_dir(path: String) -> Result<Vec<DirEntry>, String> {
    let mut entries = Vec::new();
    let mut dir = tokio::fs::read_dir(&path)
        .await
        .map_err(|e| format!("Failed to read directory: {}", e))?;

    while let Some(entry) = dir
        .next_entry()
        .await
        .map_err(|e| format!("Failed to read entry: {}", e))?
    {
        let name = entry.file_name().to_string_lossy().to_string();
        let is_dir = entry
            .file_type()
            .await
            .map(|ft| ft.is_dir())
            .unwrap_or(false);

        // Skip hidden files
        if name.starts_with('.') {
            continue;
        }

        entries.push(DirEntry { name, is_dir });
    }

    // Sort: directories first, then alphabetical
    entries.sort_by(|a, b| {
        b.is_dir.cmp(&a.is_dir).then(a.name.cmp(&b.name))
    });

    Ok(entries)
}

#[tauri::command]
pub async fn start_watching(
    path: String,
    state: State<'_, AppState>,
    app: tauri::AppHandle,
) -> Result<(), String> {
    let watcher = &state.fs_watcher;
    watcher
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .start_watching(&path, app)
        .map_err(|e| format!("Failed to start watching: {}", e))
}

#[tauri::command]
pub async fn stop_watching(
    path: String,
    state: State<'_, AppState>,
) -> Result<(), String> {
    let watcher = &state.fs_watcher;
    watcher
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .stop_watching(&path)
        .map_err(|e| format!("Failed to stop watching: {}", e))
}
