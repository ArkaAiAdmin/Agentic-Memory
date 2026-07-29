/**
 * Filesystem Watcher Crate
 *
 * Uses the `notify` crate to watch for file changes in project directories.
 * Emits Tauri events when files are created, modified, deleted, or renamed.
 */

use notify::{Config, Event, EventKind, RecommendedWatcher, RecursiveMode, Watcher};
use serde::Serialize;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use std::thread::sleep;
use tauri::{async_runtime, AppHandle, Emitter};
use thiserror::Error;

const DEBOUNCE_MS: u64 = 100;

#[derive(Error, Debug)]
pub enum FsWatcherError {
    #[error("notify error: {0}")]
    Notify(#[from] notify::Error),
    #[error("path not watched: {0}")]
    NotWatched(String),
}

#[derive(Serialize, Clone)]
pub struct FsChangeEvent {
    pub path: String,
    pub change_type: String,
    pub new_path: Option<String>,
}

struct PendingEvent {
    event: FsChangeEvent,
    scheduled_at: Instant,
}

pub struct FsWatcher {
    watchers: HashMap<PathBuf, RecommendedWatcher>,
    pending: Arc<Mutex<HashMap<String, PendingEvent>>>,
    app: Option<AppHandle>,
}

impl FsWatcher {
    pub fn new() -> Self {
        Self {
            watchers: HashMap::new(),
            pending: Arc::new(Mutex::new(HashMap::new())),
            app: None,
        }
    }

    fn flush_pending(&self) {
        let app = match &self.app {
            Some(a) => a,
            None => return,
        };

        let mut pending = match self.pending.lock() {
            Ok(p) => p,
            Err(_) => return,
        };

        let now = std::time::Instant::now();
        let ready: Vec<_> = pending
            .iter()
            .filter(|(_, p)| now.duration_since(p.scheduled_at) >= Duration::from_millis(DEBOUNCE_MS))
            .map(|(k, v)| (k.clone(), v.event.clone()))
            .collect();

        for (_, event) in &ready {
            let _ = app.emit("fs-change", event);
        }

        for (k, _) in &ready {
            pending.remove(k);
        }
    }

    pub fn start_watching(
        &mut self,
        path: &str,
        app: tauri::AppHandle,
    ) -> Result<(), FsWatcherError> {
        let watch_path = PathBuf::from(path);

        // Don't watch the same path twice
        if self.watchers.contains_key(&watch_path) {
            return Ok(());
        }

        // Store app handle for debounced emits
        if self.app.is_none() {
            self.app = Some(app.clone());
        }

        let pending = self.pending.clone();
        let app_for_flush = app.clone();
        
        let mut watcher = RecommendedWatcher::new(
            move |result: Result<Event, notify::Error>| {
                match result {
                    Ok(event) => {
                        let change_type = match event.kind {
                            EventKind::Create(_) => "created",
                            EventKind::Modify(_) => "modified",
                            EventKind::Remove(_) => "deleted",
                            _ => return,
                        };

                        for path in &event.paths {
                            let change_event = FsChangeEvent {
                                path: path.to_string_lossy().to_string(),
                                change_type: change_type.to_string(),
                                new_path: None,
                            };

                            let path_key = change_event.path.clone();
                            if let Ok(mut p) = pending.lock() {
                                p.insert(path_key, PendingEvent {
                                    event: change_event,
                                    scheduled_at: std::time::Instant::now(),
                                });
                            }
                        }
                    }
                    Err(e) => {
                        log::error!("File watcher error: {}", e);
                    }
                }
            },
            Config::default(),
        )?;

        watcher.watch(&watch_path, RecursiveMode::Recursive)?;
        self.watchers.insert(watch_path, watcher);

        // Start periodic flush task
        let pending_clone = self.pending.clone();
        async_runtime::spawn(async move {
            loop {
                std::thread::sleep(Duration::from_millis(DEBOUNCE_MS));
                
                let mut pending = match pending_clone.lock() {
                    Ok(p) => p,
                    Err(_) => continue,
                };

                let now = std::time::Instant::now();
                let ready: Vec<_> = pending
                    .iter()
                    .filter(|(_, p)| now.duration_since(p.scheduled_at) >= Duration::from_millis(DEBOUNCE_MS))
                    .map(|(k, v)| (k.clone(), v.event.clone()))
                    .collect();

                drop(pending);

                for (_, event) in &ready {
                    let _ = app_for_flush.emit("fs-change", event);
                }

                let mut pending = match pending_clone.lock() {
                    Ok(p) => p,
                    Err(_) => continue,
                };
                for (k, _) in &ready {
                    pending.remove(k);
                }
            }
        });

        log::info!("Started watching: {}", path);
        Ok(())
    }

    pub fn stop_watching(&mut self, path: &str) -> Result<(), FsWatcherError> {
        let watch_path = PathBuf::from(path);
        self.watchers
            .remove(&watch_path)
            .ok_or_else(|| FsWatcherError::NotWatched(path.to_string()))?;

        log::info!("Stopped watching: {}", path);
        Ok(())
    }
}
