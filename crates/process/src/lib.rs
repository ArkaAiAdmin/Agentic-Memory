/**
 * Sandboxed Process Execution Crate
 *
 * Manages spawning, monitoring, and killing processes.
 * Supports synchronous, background, and managed (long-lived with
 * stdout/stderr streaming) execution modes.
 */

use serde::Serialize;
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read};
use std::process::{Command, Stdio};
use std::sync::{Arc, Mutex};
use thiserror::Error;

#[derive(Error, Debug)]
pub enum ProcessError {
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("process not found: {0}")]
    NotFound(String),
    #[error("process already exited")]
    Exited,
}

#[derive(Serialize)]
pub struct SyncResult {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
}

#[derive(Serialize)]
pub struct ManagedProcessInfo {
    pub pid: u32,
    pub alive: bool,
    pub stdout: String,
    pub stderr: String,
}

pub struct ManagedProcess {
    pub child: std::process::Child,
    pub stdout: Arc<Mutex<String>>,
    pub stderr: Arc<Mutex<String>>,
}

pub struct ProcessManager {
    pub background: HashMap<String, ManagedProcess>,
}

impl ProcessManager {
    pub fn new() -> Self {
        Self {
            background: HashMap::new(),
        }
    }

    /// Run a command synchronously and return the result.
    pub fn run_sync(
        &self,
        command: &str,
        cwd: &str,
        env: Option<HashMap<String, String>>,
    ) -> Result<SyncResult, ProcessError> {
        let mut cmd = Command::new("sh");
        cmd.arg("-c")
            .arg(command)
            .current_dir(cwd)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        if let Some(env_vars) = env {
            for (key, value) in env_vars {
                cmd.env(key, value);
            }
        }

        let output = cmd.output()?;

        Ok(SyncResult {
            stdout: String::from_utf8_lossy(&output.stdout).to_string(),
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
            exit_code: output.status.code().unwrap_or(-1),
        })
    }

    /// Run a command in the background and return a process ID.
    pub fn run_background(
        &mut self,
        command: &str,
        cwd: &str,
    ) -> Result<String, ProcessError> {
        let mut child = Command::new("sh")
            .arg("-c")
            .arg(command)
            .current_dir(cwd)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;

        let stdout_buf = Arc::new(Mutex::new(String::new()));
        let stderr_buf = Arc::new(Mutex::new(String::new()));

        let stdout_clone = stdout_buf.clone();
        let stderr_clone = stderr_buf.clone();

        if let Some(stdout) = child.stdout.take() {
            std::thread::spawn(move || {
                let reader = BufReader::new(stdout);
                for line in reader.lines() {
                    if let Ok(line) = line {
                        stdout_clone.lock().unwrap().push_str(&line);
                        stdout_clone.lock().unwrap().push('\n');
                    }
                }
            });
        }

        if let Some(stderr) = child.stderr.take() {
            std::thread::spawn(move || {
                let reader = BufReader::new(stderr);
                for line in reader.lines() {
                    if let Ok(line) = line {
                        stderr_clone.lock().unwrap().push_str(&line);
                        stderr_clone.lock().unwrap().push('\n');
                    }
                }
            });
        }

        let process_id = uuid::Uuid::new_v4().to_string();
        self.background.insert(
            process_id.clone(),
            ManagedProcess {
                child,
                stdout: stdout_buf,
                stderr: stderr_buf,
            },
        );

        Ok(process_id)
    }

    /// Spawn a managed long-lived process (e.g. Python MCP server).
    /// Returns a process ID that can be used for subsequent queries.
    pub fn spawn_managed(
        &mut self,
        program: &str,
        args: &[&str],
        cwd: &str,
        env: &[(&str, &str)],
    ) -> Result<String, ProcessError> {
        let mut cmd = Command::new(program);
        cmd.args(args)
            .current_dir(cwd)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        for (key, value) in env {
            cmd.env(key, value);
        }

        let mut child = cmd.spawn()?;
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
        self.background.insert(
            process_id.clone(),
            ManagedProcess {
                child,
                stdout: stdout_buf,
                stderr: stderr_buf,
            },
        );

        Ok(process_id)
    }

    /// Get accumulated stdout from a background or managed process.
    pub fn get_stdout(&self, process_id: &str) -> Result<String, ProcessError> {
        let proc = self
            .background
            .get(process_id)
            .ok_or_else(|| ProcessError::NotFound(process_id.to_string()))?;

        let output = proc.stdout.lock().map_err(|_| ProcessError::Exited)?;
        Ok(output.clone())
    }

    /// Get accumulated stderr from a background or managed process.
    pub fn get_stderr(&self, process_id: &str) -> Result<String, ProcessError> {
        let proc = self
            .background
            .get(process_id)
            .ok_or_else(|| ProcessError::NotFound(process_id.to_string()))?;

        let output = proc.stderr.lock().map_err(|_| ProcessError::Exited)?;
        Ok(output.clone())
    }

    /// Get info about a managed process.
    pub fn get_managed_info(&self, process_id: &str) -> Result<ManagedProcessInfo, ProcessError> {
        let proc = self
            .background
            .get(process_id)
            .ok_or_else(|| ProcessError::NotFound(process_id.to_string()))?;

        let alive = proc.child.id() > 0;
        let stdout = proc.stdout.lock().map_err(|_| ProcessError::Exited)?.clone();
        let stderr = proc.stderr.lock().map_err(|_| ProcessError::Exited)?.clone();

        Ok(ManagedProcessInfo {
            pid: proc.child.id(),
            alive,
            stdout,
            stderr,
        })
    }

    /// Kill a managed or background process.
    pub fn kill(&mut self, process_id: &str) -> Result<(), ProcessError> {
        let mut proc = self
            .background
            .remove(process_id)
            .ok_or_else(|| ProcessError::NotFound(process_id.to_string()))?;

        let _ = proc.child.kill();
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_run_sync() {
        let manager = ProcessManager::new();
        let result = manager.run_sync("echo 'hello process'", ".", None).unwrap();
        assert_eq!(result.exit_code, 0);
        assert!(result.stdout.contains("hello process"));
    }

    #[test]
    fn test_run_background_and_kill() {
        let mut manager = ProcessManager::new();
        let proc_id = manager.run_background("sleep 10", ".").unwrap();
        assert!(!proc_id.is_empty());
        assert!(manager.get_managed_info(&proc_id).is_ok());
        manager.kill(&proc_id).unwrap();
        assert!(manager.get_managed_info(&proc_id).is_err());
    }

    #[test]
    fn test_spawn_managed() {
        let mut manager = ProcessManager::new();
        let proc_id = manager
            .spawn_managed("echo", &["managed test"], ".", &[])
            .unwrap();
        std::thread::sleep(std::time::Duration::from_millis(200));

        let stdout = manager.get_stdout(&proc_id).unwrap();
        assert!(stdout.contains("managed test"));
        let _ = manager.kill(&proc_id);
    }
}
