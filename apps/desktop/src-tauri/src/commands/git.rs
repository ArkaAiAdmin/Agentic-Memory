use std::sync::Arc;
use tauri::State;
use crate::AppState;
use ami_git::{CommitInfo, BranchInfo, StashEntry, BlameLine};
use serde::Serialize;
use std::time::Duration;

// ── Status & Diff ──────────────────────────────────────────────────────────

#[tauri::command]
pub async fn git_status(
    repo_path: String,
    state: State<'_, Arc<AppState>>,
) -> Result<String, String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .status(&repo_path)
            .map_err(|e| format!("Git status failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_diff(
    repo_path: String,
    file_path: Option<String>,
    state: State<'_, Arc<AppState>>,
) -> Result<String, String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .diff(&repo_path, file_path.as_deref())
            .map_err(|e| format!("Git diff failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_diff_staged(
    repo_path: String,
    file_path: Option<String>,
    state: State<'_, Arc<AppState>>,
) -> Result<String, String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .diff_staged(&repo_path, file_path.as_deref())
            .map_err(|e| format!("Git diff_staged failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_diff_unstaged(
    repo_path: String,
    file_path: Option<String>,
    state: State<'_, Arc<AppState>>,
) -> Result<String, String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .diff_unstaged(&repo_path, file_path.as_deref())
            .map_err(|e| format!("Git diff_unstaged failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

// ── Staging ────────────────────────────────────────────────────────────────

#[tauri::command]
pub async fn git_stage(
    repo_path: String,
    paths: Vec<String>,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .stage(&repo_path, &paths)
            .map_err(|e| format!("Git stage failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_unstage(
    repo_path: String,
    paths: Vec<String>,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .unstage(&repo_path, &paths)
            .map_err(|e| format!("Git unstage failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_stage_all(
    repo_path: String,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .stage_all(&repo_path)
            .map_err(|e| format!("Git stage_all failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_unstage_all(
    repo_path: String,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .unstage_all(&repo_path)
            .map_err(|e| format!("Git unstage_all failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_discard_file(
    repo_path: String,
    file_path: String,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .discard_file(&repo_path, &file_path)
            .map_err(|e| format!("Git discard failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_commit(
    repo_path: String,
    message: String,
    amend: Option<bool>,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    let amend = amend.unwrap_or(false);
    tokio::task::spawn_blocking(move || {
        let ops = state.git.lock().map_err(|e| format!("Lock error: {}", e))?;
        if amend {
            ops.commit_amend(&repo_path, &message)
                .map_err(|e| format!("Git commit_amend failed: {}", e))
        } else {
            ops.commit(&repo_path, &message)
                .map_err(|e| format!("Git commit failed: {}", e))
        }
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

// ── Log ────────────────────────────────────────────────────────────────────

#[tauri::command]
pub async fn git_log(
    repo_path: String,
    limit: u32,
    state: State<'_, Arc<AppState>>,
) -> Result<String, String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .log(&repo_path, limit)
            .map_err(|e| format!("Git log failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_log_parsed(
    repo_path: String,
    limit: u32,
    state: State<'_, Arc<AppState>>,
) -> Result<Vec<CommitInfo>, String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .log_parsed(&repo_path, limit)
            .map_err(|e| format!("Git log_parsed failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

// ── Branches ───────────────────────────────────────────────────────────────

#[tauri::command]
pub async fn git_branch(
    repo_path: String,
    state: State<'_, Arc<AppState>>,
) -> Result<String, String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .current_branch(&repo_path)
            .map_err(|e| format!("Git branch failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_branches(
    repo_path: String,
    state: State<'_, Arc<AppState>>,
) -> Result<Vec<BranchInfo>, String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .branches(&repo_path)
            .map_err(|e| format!("Git branches failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_create_branch(
    repo_path: String,
    name: String,
    start_point: Option<String>,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .create_branch(&repo_path, &name, start_point.as_deref())
            .map_err(|e| format!("Git create_branch failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_switch_branch(
    repo_path: String,
    name: String,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .switch_branch(&repo_path, &name)
            .map_err(|e| format!("Git switch_branch failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_delete_branch(
    repo_path: String,
    name: String,
    force: Option<bool>,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    let force = force.unwrap_or(false);
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .delete_branch(&repo_path, &name, force)
            .map_err(|e| format!("Git delete_branch failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_merge_branch(
    repo_path: String,
    name: String,
    state: State<'_, Arc<AppState>>,
) -> Result<MergeResult, String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        let msg = state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .merge_branch(&repo_path, &name)
            .map_err(|e| format!("Git merge failed: {}", e))?;
        Ok(MergeResult { success: true, message: msg })
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[derive(Serialize)]
pub struct MergeResult {
    pub success: bool,
    pub message: String,
}

// ── Stash ──────────────────────────────────────────────────────────────────

#[tauri::command]
pub async fn git_stash_list(
    repo_path: String,
    state: State<'_, Arc<AppState>>,
) -> Result<Vec<StashEntry>, String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .stash_list(&repo_path)
            .map_err(|e| format!("Git stash_list failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_stash_push(
    repo_path: String,
    message: Option<String>,
    include_untracked: Option<bool>,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    let include_untracked = include_untracked.unwrap_or(false);
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .stash_push(&repo_path, message.as_deref(), include_untracked)
            .map_err(|e| format!("Git stash_push failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_stash_apply(
    repo_path: String,
    index: usize,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .stash_apply(&repo_path, index)
            .map_err(|e| format!("Git stash_apply failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_stash_pop(
    repo_path: String,
    index: usize,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .stash_pop(&repo_path, index)
            .map_err(|e| format!("Git stash_pop failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

#[tauri::command]
pub async fn git_stash_drop(
    repo_path: String,
    index: usize,
    state: State<'_, Arc<AppState>>,
) -> Result<(), String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .stash_drop(&repo_path, index)
            .map_err(|e| format!("Git stash_drop failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

// ── Blame ──────────────────────────────────────────────────────────────────

#[tauri::command]
pub async fn git_blame(
    repo_path: String,
    file_path: String,
    state: State<'_, Arc<AppState>>,
) -> Result<Vec<BlameLine>, String> {
    let state = Arc::clone(state.inner());
    tokio::task::spawn_blocking(move || {
        state.git.lock()
            .map_err(|e| format!("Lock error: {}", e))?
            .blame(&repo_path, &file_path)
            .map_err(|e| format!("Git blame failed: {}", e))
    }).await.map_err(|e| format!("Task join error: {}", e))?
}

// ── Remote (via git CLI for auth support) ──────────────────────────────────

#[derive(Serialize)]
pub struct RemoteResult {
    pub success: bool,
    pub message: String,
}

#[tauri::command]
pub async fn git_push(
    repo_path: String,
    remote: Option<String>,
    branch: Option<String>,
) -> Result<RemoteResult, String> {
    let mut cmd = tokio::process::Command::new("git");
    cmd.current_dir(&repo_path).arg("push");
    if let Some(r) = &remote {
        cmd.arg(r);
    }
    if let Some(b) = &branch {
        cmd.arg(b);
    }
    let output = tokio::time::timeout(Duration::from_secs(120), cmd.output())
        .await
        .map_err(|_| "Git push timed out after 120 seconds".to_string())?
        .map_err(|e| format!("Failed to run git push: {}", e))?;
    let msg = String::from_utf8_lossy(&output.stdout).to_string()
        + &String::from_utf8_lossy(&output.stderr);
    Ok(RemoteResult { success: output.status.success(), message: msg })
}

#[tauri::command]
pub async fn git_pull(
    repo_path: String,
    remote: Option<String>,
    branch: Option<String>,
) -> Result<RemoteResult, String> {
    let mut cmd = tokio::process::Command::new("git");
    cmd.current_dir(&repo_path).arg("pull");
    if let Some(r) = &remote {
        cmd.arg(r);
    }
    if let Some(b) = &branch {
        cmd.arg(b);
    }
    let output = tokio::time::timeout(Duration::from_secs(120), cmd.output())
        .await
        .map_err(|_| "Git pull timed out after 120 seconds".to_string())?
        .map_err(|e| format!("Failed to run git pull: {}", e))?;
    let msg = String::from_utf8_lossy(&output.stdout).to_string()
        + &String::from_utf8_lossy(&output.stderr);
    Ok(RemoteResult { success: output.status.success(), message: msg })
}

#[tauri::command]
pub async fn git_fetch(
    repo_path: String,
    remote: Option<String>,
) -> Result<(), String> {
    let mut cmd = tokio::process::Command::new("git");
    cmd.current_dir(&repo_path).arg("fetch");
    if let Some(r) = &remote {
        cmd.arg(r);
    }
    let output = tokio::time::timeout(Duration::from_secs(120), cmd.output())
        .await
        .map_err(|_| "Git fetch timed out after 120 seconds".to_string())?
        .map_err(|e| format!("Failed to run git fetch: {}", e))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(String::from_utf8_lossy(&output.stderr).to_string())
    }
}
