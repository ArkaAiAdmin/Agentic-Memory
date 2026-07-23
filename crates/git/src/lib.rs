/**
 * Git Operations Crate
 *
 * Uses `git2` (libgit2 bindings) for git operations:
 * - Status, diff, log, branch operations
 * - Commit message generation (agent-assisted)
 * - Change tracking
 */

use git2::{DiffOptions, Repository, Sort};
use thiserror::Error;

#[derive(Error, Debug)]
pub enum GitError {
    #[error("git error: {0}")]
    Git(#[from] git2::Error),
    #[error("not a git repository: {0}")]
    NotRepo(String),
}

pub struct GitOps;

impl GitOps {
    pub fn new() -> Self {
        Self
    }

    pub fn status(&self, repo_path: &str) -> Result<String, GitError> {
        let repo = Repository::open(repo_path)?;
        let statuses = repo.statuses(None)?;

        let mut output = String::new();
        for entry in statuses.iter() {
            let path = entry.path().unwrap_or("?");
            let status = entry.status();

            let symbol = if status.is_index_new() || status.is_wt_new() {
                "?"
            } else if status.is_index_modified() {
                "M"
            } else if status.is_wt_modified() {
                "m"
            } else if status.is_index_deleted() {
                "D"
            } else if status.is_wt_deleted() {
                "d"
            } else if status.is_index_renamed() {
                "R"
            } else {
                " "
            };

            output.push_str(&format!("{} {}\n", symbol, path));
        }

        Ok(output)
    }

    pub fn diff(&self, repo_path: &str, file_path: Option<&str>) -> Result<String, GitError> {
        let repo = Repository::open(repo_path)?;
        let head = repo.head()?.peel_to_tree()?;

        let mut opts = DiffOptions::new();
        if let Some(path) = file_path {
            opts.pathspec(path);
        }

        let diff = repo.diff_tree_to_workdir_with_index(Some(&head), Some(&mut opts))?;

        let mut output = String::new();
        diff.print(git2::DiffFormat::Patch, |_delta, _hunk, line| {
            let prefix = match line.origin() {
                '+' => "+",
                '-' => "-",
                ' ' => " ",
                _ => " ",
            };
            output.push_str(prefix);
            if let Ok(content) = std::str::from_utf8(line.content()) {
                output.push_str(content);
            }
            true
        })?;

        Ok(output)
    }

    pub fn log(&self, repo_path: &str, limit: u32) -> Result<String, GitError> {
        let repo = Repository::open(repo_path)?;
        let mut revwalk = repo.revwalk()?;
        revwalk.push_head()?;
        revwalk.set_sorting(Sort::TIME)?;

        let mut output = String::new();
        let mut count = 0;

        for oid in revwalk {
            if count >= limit {
                break;
            }

            let oid = oid?;
            let commit = repo.find_commit(oid)?;

            output.push_str(&format!(
                "{} {} ({})\n",
                &oid.to_string()[..8],
                commit.summary().unwrap_or("(no message)"),
                commit.author().name().unwrap_or("unknown"),
            ));

            count += 1;
        }

        Ok(output)
    }

    pub fn commit(&self, repo_path: &str, message: &str) -> Result<(), GitError> {
        let repo = Repository::open(repo_path)?;

        // Stage all changes
        let mut index = repo.index()?;
        index.add_all(["*"].iter(), git2::IndexAddOption::DEFAULT, None)?;
        index.write()?;

        let tree_id = index.write_tree()?;
        let tree = repo.find_tree(tree_id)?;

        let sig = repo.signature()?;

        if repo.head().is_err() {
            // Initial commit
            repo.commit(Some("HEAD"), &sig, &sig, message, &tree, &[])?;
        } else {
            let parent = repo.head()?.peel_to_commit()?;
            repo.commit(Some("HEAD"), &sig, &sig, message, &tree, &[&parent])?;
        }

        Ok(())
    }

    pub fn current_branch(&self, repo_path: &str) -> Result<String, GitError> {
        let repo = Repository::open(repo_path)?;
        let head = repo.head()?;
        let branch_name = head
            .shorthand()
            .unwrap_or("HEAD")
            .to_string();
        Ok(branch_name)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_git_ops_in_temp_repo() {
        let temp_dir = std::env::temp_dir().join(format!("test_git_repo_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&temp_dir).unwrap();

        let repo = Repository::init(&temp_dir).unwrap();
        let file_path = temp_dir.join("hello.txt");
        std::fs::write(&file_path, "hello git world").unwrap();

        let ops = GitOps::new();
        let status = ops.status(temp_dir.to_str().unwrap()).unwrap();
        assert!(status.contains("hello.txt"));

        // Configure sig and commit
        let mut config = repo.config().unwrap();
        config.set_str("user.name", "Test User").unwrap();
        config.set_str("user.email", "test@example.com").unwrap();

        ops.commit(temp_dir.to_str().unwrap(), "Initial test commit").unwrap();

        let branch = ops.current_branch(temp_dir.to_str().unwrap()).unwrap();
        assert!(!branch.is_empty());

        let log = ops.log(temp_dir.to_str().unwrap(), 5).unwrap();
        assert!(log.contains("Initial test commit"));

        let _ = std::fs::remove_dir_all(&temp_dir);
    }
}
