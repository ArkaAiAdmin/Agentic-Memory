use ami_process::ManagedProcess;
use serde::Serialize;
use std::io::{BufReader, Read};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use tauri::State;
use crate::AppState;

#[derive(Serialize)]
pub struct MemoryBridgeStatus {
    pub process_id: String,
    pub pid: u32,
    pub alive: bool,
    pub stdout: String,
    pub stderr: String,
}

#[tauri::command]
pub async fn start_memory_bridge(
    memory_dir: String,
    python_path: Option<String>,
    state: State<'_, AppState>,
) -> Result<String, String> {
    let python = python_path.unwrap_or_else(|| "python3".to_string());

    let mut cmd = Command::new(&python);
    cmd.args(["-m", "agentic_memory", "--transport", "stdio"])
        .current_dir(&memory_dir)
        .env("AGENTIC_MEMORY_DIR", &memory_dir)
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = cmd.spawn().map_err(|e| format!("Failed to spawn memory bridge: {}", e))?;

    let _pid = child.id();
    let stdout_buf = Arc::new(Mutex::new(String::new()));
    let stderr_buf = Arc::new(Mutex::new(String::new()));

    let stdout_clone = stdout_buf.clone();
    let stderr_clone = stderr_buf.clone();

    if let Some(stdout) = child.stdout.take() {
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut buf = [0u8; 4096];
            loop {
                match reader.read(&mut buf) {
                    Ok(n) if n > 0 => {
                        let chunk = String::from_utf8_lossy(&buf[..n]).to_string();
                        stdout_clone.lock().unwrap().push_str(&chunk);
                    }
                    _ => break,
                }
            }
        });
    }

    if let Some(stderr) = child.stderr.take() {
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stderr);
            let mut buf = [0u8; 4096];
            loop {
                match reader.read(&mut buf) {
                    Ok(n) if n > 0 => {
                        let chunk = String::from_utf8_lossy(&buf[..n]).to_string();
                        stderr_clone.lock().unwrap().push_str(&chunk);
                    }
                    _ => break,
                }
            }
        });
    }

    let process_id = uuid::Uuid::new_v4().to_string();

    state
        .process
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .background
        .insert(
            process_id.clone(),
            ManagedProcess {
                child,
                stdout: stdout_buf,
                stderr: stderr_buf,
            },
        );

    Ok(process_id)
}

#[tauri::command]
pub async fn stop_memory_bridge(
    process_id: String,
    state: State<'_, AppState>,
) -> Result<(), String> {
    state
        .process
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .kill(&process_id)
        .map_err(|e| format!("Failed to stop memory bridge: {}", e))
}

#[tauri::command]
pub async fn get_memory_bridge_status(
    process_id: String,
    state: State<'_, AppState>,
) -> Result<MemoryBridgeStatus, String> {
    let info = state
        .process
        .lock()
        .map_err(|e| format!("Lock error: {}", e))?
        .get_managed_info(&process_id)
        .map_err(|e| format!("Failed to get status: {}", e))?;

    Ok(MemoryBridgeStatus {
        process_id,
        pid: info.pid,
        alive: info.alive,
        stdout: info.stdout,
        stderr: info.stderr,
    })
}
