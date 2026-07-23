/**
 * Tauri Backend — Main Entry Point
 *
 * Registers all Rust commands (invoke handlers) and event emitters.
 * Bridges the React frontend to the native Rust crates for:
 * - Filesystem watching (notify)
 * - PTY management (portable-pty)
 * - Git operations (git2)
 * - Sandboxed process execution
 */

mod commands;

use std::sync::Mutex;

/// Application state shared across all Tauri commands.
pub struct AppState {
    pub pty_manager: Mutex<ami_pty::PtyManager>,
    pub fs_watcher: Mutex<ami_fs_watcher::FsWatcher>,
    pub git: Mutex<ami_git::GitOps>,
    pub process: Mutex<ami_process::ProcessManager>,
}

pub fn main() {
    env_logger::init();

    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState {
            pty_manager: Mutex::new(ami_pty::PtyManager::new()),
            fs_watcher: Mutex::new(ami_fs_watcher::FsWatcher::new()),
            git: Mutex::new(ami_git::GitOps::new()),
            process: Mutex::new(ami_process::ProcessManager::new()),
        })
        .invoke_handler(tauri::generate_handler![
            // Filesystem
            commands::fs::read_file,
            commands::fs::write_file,
            commands::fs::list_dir,
            commands::fs::start_watching,
            commands::fs::stop_watching,
            // Terminal (PTY)
            commands::terminal::create_pty,
            commands::terminal::write_pty,
            commands::terminal::resize_pty,
            commands::terminal::destroy_pty,
            // Git
            commands::git::git_status,
            commands::git::git_diff,
            commands::git::git_log,
            commands::git::git_commit,
            commands::git::git_branch,
            // Process
            commands::process::run_command,
            commands::process::run_background,
            commands::process::get_output,
            commands::process::get_stdout,
            commands::process::get_stderr,
            commands::process::get_managed_info,
            commands::process::is_process_alive,
            commands::process::write_process_stdin,
            commands::process::kill_process,
            // Memory Bridge
            commands::memory::start_memory_bridge,
            commands::memory::stop_memory_bridge,
            commands::memory::get_memory_bridge_status,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
