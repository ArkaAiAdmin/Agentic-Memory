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

/// Kill any existing memory_mcp.py processes with --agent-id IDE
/// to prevent flock conflicts that cause handshake timeouts.
fn kill_existing_mcp_processes(agent_id: &str) {
    let pattern = format!("memory_mcp.py.*--agent-id.*{}", agent_id);
    let pattern_sig9 = format!("memory_mcp.py.*--agent-id.*{}", agent_id);
    // Use pkill to terminate orphan memory_mcp processes with agent-id
    let _ = Command::new("pkill")
        .args(["-f", &pattern])
        .output();
    // Wait for graceful shutdown
    std::thread::sleep(std::time::Duration::from_millis(300));
    // Force-kill any survivors
    let _ = Command::new("pkill")
        .args(["-9", "-f", &pattern_sig9])
        .output();
    // Wait for OS to reclaim resources
    std::thread::sleep(std::time::Duration::from_millis(300));
    // Remove stale lock file
    let lock_path = std::path::Path::new(&std::env::var("HOME").unwrap_or_default())
        .join(format!(".config/agentic-memory/memory/.mcp_server.lock.{}", agent_id.to_lowercase()));
    let _ = std::fs::remove_file(&lock_path);
}

#[tauri::command]
pub async fn start_memory_bridge(
    memory_dir: String,
    python_path: Option<String>,
    agent_id: Option<String>,
    state: State<'_, Arc<AppState>>,
) -> Result<String, String> {
    let effective_agent_id = agent_id.unwrap_or_else(|| "IDE".to_string());
    // Kill any orphan MCP processes that might hold the flock
    kill_existing_mcp_processes(&effective_agent_id);

    // Try multiple Python paths since .app bundles have limited PATH
    let python = if let Some(p) = python_path {
        p
    } else {
        // Prefer the project's venv python (has sentence_transformers, spacy, etc.)
        let venv_python = format!("{}/venv/bin/python", memory_dir);
        let dot_venv_python = format!("{}/.venv/bin/python", memory_dir);
        let candidates: Vec<&str> = vec![
            "/opt/homebrew/bin/python3",
            "/opt/homebrew/opt/python@3.14/bin/python3.14",
            "/opt/homebrew/opt/python@3.13/bin/python3.13",
            "/opt/homebrew/opt/python@3.12/bin/python3.12",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ];
        if std::path::Path::new(&venv_python).exists() {
            venv_python
        } else if std::path::Path::new(&dot_venv_python).exists() {
            dot_venv_python
        } else {
            candidates.iter()
                .find(|p| std::path::Path::new(p).exists())
                .map(|s| s.to_string())
                .unwrap_or_else(|| "python3".to_string())
        }
    };

    // Use absolute path for the script
    let script_path = std::path::Path::new(&memory_dir).join("memory_mcp.py");
    if !script_path.exists() {
        return Err(format!("Script not found: {}", script_path.display()));
    }

    let mut cmd = Command::new(&python);
    cmd.arg(script_path.to_str().unwrap())
        .arg("--agent-id")
        .arg(&effective_agent_id)
        .current_dir(&memory_dir)
        .env("AGENTIC_MEMORY_DIR", &memory_dir)
        .env("PYTHONUNBUFFERED", "1")
        .env("MEMORY_AUTH_MODE", "open")
        .env("MEMORY_AGENT_ID", &effective_agent_id)
        .env("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    // Log the exact spawn config for diagnosis
    eprintln!("[MemoryBridge] Spawning: python={} script={} cwd={} agent_id={}", python, script_path.display(), memory_dir, effective_agent_id);

    let mut child = cmd.spawn().map_err(|e| format!("Failed to spawn memory bridge: {}", e))?;
    eprintln!("[MemoryBridge] Spawned PID={}", child.id());

    // Quick liveness check: if the process exited during import, capture exit status
    std::thread::sleep(std::time::Duration::from_millis(200));
    match child.try_wait() {
        Ok(Some(status)) => {
            eprintln!("[MemoryBridge] Process exited immediately with status: {}", status);
        }
        Ok(None) => {
            eprintln!("[MemoryBridge] Process still alive after 200ms");
        }
        Err(e) => {
            eprintln!("[MemoryBridge] try_wait error: {}", e);
        }
    }

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
                        stdout_clone.lock().unwrap_or_else(|poison| poison.into_inner()).push_str(&chunk);
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
                        stderr_clone.lock().unwrap_or_else(|poison| poison.into_inner()).push_str(&chunk);
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
    state: State<'_, Arc<AppState>>,
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
    state: State<'_, Arc<AppState>>,
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
