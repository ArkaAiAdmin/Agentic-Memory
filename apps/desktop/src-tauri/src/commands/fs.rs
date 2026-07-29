use serde::Serialize;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use tauri::State;
use crate::AppState;

const FILE_SIZE_LIMIT: u64 = 50 * 1024 * 1024;

fn validate_path(path: &str, workspace_root: &str) -> Result<PathBuf, String> {
    let root = PathBuf::from(workspace_root).canonicalize()
        .map_err(|e| format!("Invalid workspace root: {}", e))?;
    let path_buf = if Path::new(path).is_absolute() {
        PathBuf::from(path)
    } else {
        root.join(path)
    }
    .canonicalize()
    .map_err(|e| format!("Invalid path: {}", e))?;
    if !path_buf.starts_with(&root) {
        return Err(format!("Path is outside workspace: {}", path));
    }
    Ok(path_buf)
}

fn workspace_root() -> Result<String, String> {
    std::env::current_dir()
        .map_err(|e| format!("Failed to get current directory: {}", e))?
        .to_str()
        .map(|s| s.to_string())
        .ok_or_else(|| "Failed to convert path to string".to_string())
}

#[tauri::command]
pub async fn read_file(path: String) -> Result<String, String> {
    let root = workspace_root()?;
    let canonical = validate_path(&path, &root)?;

    let metadata = tokio::fs::metadata(&canonical)
        .await
        .map_err(|e| format!("Failed to read file metadata: {}", e))?;
    if metadata.len() > FILE_SIZE_LIMIT {
        return Err(format!(
            "File size {} exceeds limit of {} bytes",
            metadata.len(),
            FILE_SIZE_LIMIT
        ));
    }

    tokio::fs::read_to_string(&canonical)
        .await
        .map_err(|e| format!("Failed to read file: {}", e))
}

#[tauri::command]
pub async fn write_file(path: String, content: String) -> Result<(), String> {
    let root = workspace_root()?;
    let canonical = validate_path(&path, &root)?;

    if let Some(parent) = canonical.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(|e| format!("Failed to create directory: {}", e))?;
    }

    tokio::fs::write(&canonical, &content)
        .await
        .map_err(|e| format!("Failed to write file: {}", e))
}

#[tauri::command]
pub async fn delete_file(path: String) -> Result<(), String> {
    let root = workspace_root()?;
    let canonical = validate_path(&path, &root)?;

    tokio::fs::remove_file(&canonical)
        .await
        .map_err(|e| format!("Failed to delete file: {}", e))
}

#[derive(Serialize)]
pub struct DirEntry {
    pub name: String,
    pub is_dir: bool,
}

#[tauri::command]
pub async fn list_dir(path: String, show_hidden: Option<bool>) -> Result<Vec<DirEntry>, String> {
    let root = workspace_root()?;
    let canonical = validate_path(&path, &root)?;
    let show_hidden = show_hidden.unwrap_or(false);

    let mut entries = Vec::new();
    let mut dir = tokio::fs::read_dir(&canonical)
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

        if !show_hidden && name.starts_with('.') {
            continue;
        }

        entries.push(DirEntry { name, is_dir });
    }

    entries.sort_by(|a, b| {
        b.is_dir.cmp(&a.is_dir).then(a.name.cmp(&b.name))
    });

    Ok(entries)
}

#[tauri::command]
pub async fn start_watching(
    path: String,
    state: State<'_, Arc<AppState>>,
    app: tauri::AppHandle,
) -> Result<(), String> {
    let root = workspace_root()?;
    let canonical = validate_path(&path, &root)?;
    let canonical_str = canonical.to_str()
        .ok_or_else(|| "Failed to convert path to string".to_string())?
        .to_string();

    let watcher = &state.fs_watcher;
    watcher
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .start_watching(&canonical_str, app)
        .map_err(|e| format!("Failed to start watching: {}", e))
}

#[tauri::command]
pub async fn stop_watching(
    path: String,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let root = workspace_root()?;
    let canonical = validate_path(&path, &root)?;
    let canonical_str = canonical.to_str()
        .ok_or_else(|| "Failed to convert path to string".to_string())?
        .to_string();

    let watcher = &state.fs_watcher;
    watcher
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .stop_watching(&canonical_str)
        .map_err(|e| format!("Failed to stop watching: {}", e))
}
