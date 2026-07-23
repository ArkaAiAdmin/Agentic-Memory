use serde::Serialize;
use std::io::Write;
use tauri::State;
use crate::AppState;

#[derive(Serialize)]
pub struct CommandResult {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
}

#[derive(Serialize)]
pub struct ManagedProcessStatus {
    pub process_id: String,
    pub pid: u32,
    pub alive: bool,
    pub stdout: String,
    pub stderr: String,
}

#[tauri::command]
pub async fn run_command(
    command: String,
    cwd: String,
    env: Option<std::collections::HashMap<String, String>>,
    state: State<'_, AppState>,
) -> Result<CommandResult, String> {
    let proc_mgr = &state.process;
    let result = proc_mgr
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .run_sync(&command, &cwd, env)
        .map_err(|e| format!("Command failed: {}", e))?;

    Ok(CommandResult {
        stdout: result.stdout,
        stderr: result.stderr,
        exit_code: result.exit_code,
    })
}

#[tauri::command]
pub async fn run_background(
    command: String,
    cwd: String,
    state: State<'_, AppState>,
) -> Result<String, String> {
    let proc_mgr = &state.process;
    proc_mgr
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .run_background(&command, &cwd)
        .map_err(|e| format!("Background command failed: {}", e))
}

#[tauri::command]
pub async fn get_output(
    process_id: String,
    state: State<'_, AppState>,
) -> Result<String, String> {
    let proc_mgr = &state.process;
    proc_mgr
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .get_stdout(&process_id)
        .map_err(|e| format!("Get output failed: {}", e))
}

#[tauri::command]
pub async fn get_stdout(
    process_id: String,
    state: State<'_, AppState>,
) -> Result<String, String> {
    let proc_mgr = &state.process;
    proc_mgr
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .get_stdout(&process_id)
        .map_err(|e| format!("Get stdout failed: {}", e))
}

#[tauri::command]
pub async fn get_stderr(
    process_id: String,
    state: State<'_, AppState>,
) -> Result<String, String> {
    let proc_mgr = &state.process;
    proc_mgr
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .get_stderr(&process_id)
        .map_err(|e| format!("Get stderr failed: {}", e))
}

#[tauri::command]
pub async fn get_managed_info(
    process_id: String,
    state: State<'_, AppState>,
) -> Result<ManagedProcessStatus, String> {
    let proc_mgr = &state.process;
    let info = proc_mgr
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .get_managed_info(&process_id)
        .map_err(|e| format!("Get managed info failed: {}", e))?;

    Ok(ManagedProcessStatus {
        process_id,
        pid: info.pid,
        alive: info.alive,
        stdout: info.stdout,
        stderr: info.stderr,
    })
}

#[tauri::command]
pub async fn is_process_alive(
    process_id: String,
    state: State<'_, AppState>,
) -> Result<bool, String> {
    let proc_mgr = &state.process;
    let info = proc_mgr
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .get_managed_info(&process_id)
        .map_err(|e| format!("Process not found: {}", e))?;

    Ok(info.alive)
}

#[tauri::command]
pub async fn kill_process(
    process_id: String,
    state: State<'_, AppState>,
) -> Result<(), String> {
    let proc_mgr = &state.process;
    proc_mgr
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .kill(&process_id)
        .map_err(|e| format!("Kill failed: {}", e))
}

#[tauri::command]
pub async fn write_process_stdin(
    process_id: String,
    data: String,
    state: State<'_, AppState>,
) -> Result<(), String> {
    let proc_mgr = &state.process;
    let mut guard = proc_mgr
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?;

    let proc = guard
        .background
        .get_mut(&process_id)
        .ok_or_else(|| format!("Process not found: {}", process_id))?;

    if let Some(stdin) = proc.child.stdin.as_mut() {
        stdin
            .write_all(data.as_bytes())
            .map_err(|e| format!("Write failed: {}", e))?;
        Ok(())
    } else {
        Err("Process stdin is not available".to_string())
    }
}
