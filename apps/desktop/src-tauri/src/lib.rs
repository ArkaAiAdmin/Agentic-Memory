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

use std::sync::{Arc, Mutex};

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
        .plugin(tauri_plugin_process::init())
        .manage(Arc::new(AppState {
            pty_manager: Mutex::new(ami_pty::PtyManager::new()),
            fs_watcher: Mutex::new(ami_fs_watcher::FsWatcher::new()),
            git: Mutex::new(ami_git::GitOps::new()),
            process: Mutex::new(ami_process::ProcessManager::new()),
        }))
        .invoke_handler(tauri::generate_handler![
            // Filesystem
            commands::fs::read_file,
            commands::fs::write_file,
            commands::fs::delete_file,
            commands::fs::list_dir,
            commands::fs::start_watching,
            commands::fs::stop_watching,
            // Terminal (PTY)
            commands::terminal::create_pty,
            commands::terminal::write_pty,
            commands::terminal::resize_pty,
            commands::terminal::destroy_pty,
            // Git — status & diff
            commands::git::git_status,
            commands::git::git_diff,
            commands::git::git_diff_staged,
            commands::git::git_diff_unstaged,
            // Git — staging
            commands::git::git_stage,
            commands::git::git_unstage,
            commands::git::git_stage_all,
            commands::git::git_unstage_all,
            commands::git::git_discard_file,
            // Git — commit
            commands::git::git_commit,
            // Git — log
            commands::git::git_log,
            commands::git::git_log_parsed,
            // Git — branches
            commands::git::git_branch,
            commands::git::git_branches,
            commands::git::git_create_branch,
            commands::git::git_switch_branch,
            commands::git::git_delete_branch,
            commands::git::git_merge_branch,
            // Git — stash
            commands::git::git_stash_list,
            commands::git::git_stash_push,
            commands::git::git_stash_apply,
            commands::git::git_stash_pop,
            commands::git::git_stash_drop,
            // Git — blame
            commands::git::git_blame,
            // Git — remote
            commands::git::git_push,
            commands::git::git_pull,
            commands::git::git_fetch,
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
            // LLM HTTP transport (CORS-free, off the webview)
            commands::llm::llm_fetch,
            // Secure secret store (OS keychain)
            commands::secret::secret_set,
            commands::secret::secret_get,
            commands::secret::secret_has,
            commands::secret::secret_delete,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
