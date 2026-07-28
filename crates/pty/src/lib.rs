/**
 * PTY Crate — Pseudo-Terminal Management
 *
 * Uses `portable-pty` for cross-platform terminal emulation.
 * Manages multiple PTY instances, each identified by a UUID.
 * Output is streamed to the frontend via Tauri events.
 */

use portable_pty::{native_pty_system, CommandBuilder, PtySize};
use serde::Serialize;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::sync::{Arc, Mutex};
use tauri::Emitter;
use thiserror::Error;

#[derive(Error, Debug)]
pub enum PtyError {
    #[error("PTY error: {0}")]
    Io(#[from] std::io::Error),
    #[error("PTY not found: {0}")]
    NotFound(String),
    #[error("portable-pty error: {0}")]
    PortablePty(String),
}

#[derive(Serialize, Clone)]
pub struct PtyOutputEvent {
    pub pty_id: String,
    pub data: String,
}

#[derive(Serialize, Clone)]
pub struct PtyExitEvent {
    pub pty_id: String,
    pub exit_code: i32,
}

struct PtyInstance {
    pty: Box<dyn portable_pty::MasterPty + Send>,
    writer: Box<dyn Write + Send>,
    _child: Box<dyn portable_pty::Child + Send>,
}

pub struct PtyManager {
    instances: HashMap<String, Arc<Mutex<PtyInstance>>>,
}

impl PtyManager {
    pub fn new() -> Self {
        Self {
            instances: HashMap::new(),
        }
    }

    pub fn create(
        &mut self,
        cwd: &str,
        cols: u16,
        rows: u16,
        app: Option<tauri::AppHandle>,
    ) -> Result<String, PtyError> {
        let pty_system = native_pty_system();

        let pair = pty_system
            .openpty(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| PtyError::PortablePty(e.to_string()))?;

        let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".to_string());
        let mut cmd = CommandBuilder::new(&shell);
        cmd.cwd(cwd);

        let child = pair
            .slave
            .spawn_command(cmd)
            .map_err(|e| PtyError::PortablePty(e.to_string()))?;

        let writer = pair
            .master
            .take_writer()
            .map_err(|e| PtyError::PortablePty(e.to_string()))?;

        let mut reader = pair
            .master
            .try_clone_reader()
            .map_err(|e| PtyError::PortablePty(e.to_string()))?;

        let pty_id = uuid::Uuid::new_v4().to_string();

        if let Some(app_handle) = app {
            let pty_id_clone = pty_id.clone();
            std::thread::spawn(move || {
                let mut buf = [0u8; 4096];
                loop {
                    match reader.read(&mut buf) {
                        Ok(n) if n > 0 => {
                            let data = String::from_utf8_lossy(&buf[..n]).to_string();
                            let _ = app_handle.emit(
                                "pty-output",
                                PtyOutputEvent {
                                    pty_id: pty_id_clone.clone(),
                                    data,
                                },
                            );
                        }
                        _ => break,
                    }
                }
            });
        }

        let instance = Arc::new(Mutex::new(PtyInstance {
            pty: pair.master,
            writer,
            _child: child,
        }));

        self.instances.insert(pty_id.clone(), instance);

        Ok(pty_id)
    }

    pub fn write(&self, pty_id: &str, data: &[u8]) -> Result<(), PtyError> {
        let instance = self
            .instances
            .get(pty_id)
            .ok_or_else(|| PtyError::NotFound(pty_id.to_string()))?;

        let mut guard = instance
            .lock()
            .map_err(|_| PtyError::NotFound("lock error".to_string()))?;

        guard
            .writer
            .write_all(data)
            .map_err(PtyError::Io)?;

        Ok(())
    }

    pub fn resize(&self, pty_id: &str, cols: u16, rows: u16) -> Result<(), PtyError> {
        let instance = self
            .instances
            .get(pty_id)
            .ok_or_else(|| PtyError::NotFound(pty_id.to_string()))?;

        let guard = instance
            .lock()
            .map_err(|_| PtyError::NotFound("lock error".to_string()))?;

        guard
            .pty
            .resize(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| PtyError::PortablePty(e.to_string()))?;

        Ok(())
    }

    pub fn destroy(&mut self, pty_id: &str) -> Result<(), PtyError> {
        self.instances
            .remove(pty_id)
            .ok_or_else(|| PtyError::NotFound(pty_id.to_string()))?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pty_lifecycle() {
        let mut manager = PtyManager::new();
        let pty_id = manager.create(".", 80, 24, None).unwrap();
        assert!(!pty_id.is_empty());

        assert!(manager.write(&pty_id, b"echo test\n").is_ok());
        assert!(manager.resize(&pty_id, 100, 30).is_ok());
        assert!(manager.destroy(&pty_id).is_ok());
        assert!(manager.destroy(&pty_id).is_err());
    }
}
