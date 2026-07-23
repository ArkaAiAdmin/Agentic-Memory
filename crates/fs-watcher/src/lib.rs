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
use tauri::Emitter;
use thiserror::Error;

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

pub struct FsWatcher {
    watchers: HashMap<PathBuf, RecommendedWatcher>,
}

impl FsWatcher {
    pub fn new() -> Self {
        Self {
            watchers: HashMap::new(),
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

        let app_clone = app.clone();
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

                            // Emit Tauri event
                            let _ = app_clone.emit("fs-change", &change_event);
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
