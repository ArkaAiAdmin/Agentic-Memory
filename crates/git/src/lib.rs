/**
 * Git Operations Crate
 *
 * Uses `git2` (libgit2 bindings) for git operations:
 * - Status, diff (staged/unstaged), log, branch operations
 * - Stage/unstage files, stash, blame
 * - Structured output for frontend consumption
 */

use git2::{BranchType, DiffOptions, Repository, Sort, StatusOptions};
use serde::Serialize;
use thiserror::Error;

#[derive(Error, Debug)]
pub enum GitError {
    #[error("git error: {0}")]
    Git(#[from] git2::Error),
    #[error("not a git repository: {0}")]
    NotRepo(String),
}

#[derive(Serialize, Clone, Debug)]
pub struct CommitInfo {
    pub hash: String,
    pub short_hash: String,
    pub author: String,
    pub email: String,
    pub date: i64,
    pub message: String,
    pub parents: Vec<String>,
    pub refs: Vec<String>,
}

#[derive(Serialize, Clone, Debug)]
pub struct BranchInfo {
    pub name: String,
    pub is_current: bool,
    pub is_remote: bool,
    pub upstream: Option<String>,
    pub last_commit_hash: Option<String>,
    pub last_commit_message: Option<String>,
}

#[derive(Serialize, Clone, Debug)]
pub struct StashEntry {
    pub index: usize,
    pub message: String,
    pub date: i64,
    pub branch: String,
}

#[derive(Serialize, Clone, Debug)]
pub struct BlameLine {
    pub line_num: usize,
    pub hash: String,
    pub author: String,
    pub date: i64,
    pub content: String,
}

pub struct GitOps;

impl GitOps {
    pub fn new() -> Self {
        Self
    }

    // ── Status ──────────────────────────────────────────────────────────────

    pub fn status(&self, repo_path: &str) -> Result<String, GitError> {
        let repo = Repository::open(repo_path)?;
        let statuses = repo.statuses(None)?;

        let mut output = String::new();
        for entry in statuses.iter() {
            let path = entry.path().unwrap_or("?");
            let status = entry.status();

            let idx = if status.is_index_new() {
                'A'
            } else if status.is_index_modified() {
                'M'
            } else if status.is_index_deleted() {
                'D'
            } else if status.is_index_renamed() {
                'R'
            } else {
                ' '
            };

            let wt = if status.is_wt_new() {
                '?'
            } else if status.is_wt_modified() {
                'M'
            } else if status.is_wt_deleted() {
                'D'
            } else if status.is_wt_renamed() {
                'R'
            } else {
                ' '
            };

            output.push_str(&format!("{}{} {}\n", idx, wt, path));
        }

        Ok(output)
    }

    // ── Diff ────────────────────────────────────────────────────────────────

    pub fn diff(&self, repo_path: &str, file_path: Option<&str>) -> Result<String, GitError> {
        let repo = Repository::open(repo_path)?;
        let mut opts = DiffOptions::new();
        if let Some(path) = file_path {
            opts.pathspec(path);
        }

        let head_tree = repo.head().ok().and_then(|h| h.peel_to_tree().ok());
        let diff = if let Some(head) = &head_tree {
            repo.diff_tree_to_workdir_with_index(Some(head), Some(&mut opts))?
        } else {
            repo.diff_index_to_workdir(None, Some(&mut opts))?
        };

        Self::diff_to_string(&diff)
    }

    pub fn diff_staged(&self, repo_path: &str, file_path: Option<&str>) -> Result<String, GitError> {
        let repo = Repository::open(repo_path)?;
        let mut opts = DiffOptions::new();
        if let Some(path) = file_path {
            opts.pathspec(path);
        }

        let head_tree = repo.head().ok().and_then(|h| h.peel_to_tree().ok());
        let diff = repo.diff_tree_to_index(head_tree.as_ref(), None, Some(&mut opts))?;
        Self::diff_to_string(&diff)
    }

    pub fn diff_unstaged(&self, repo_path: &str, file_path: Option<&str>) -> Result<String, GitError> {
        let repo = Repository::open(repo_path)?;
        let mut opts = DiffOptions::new();
        if let Some(path) = file_path {
            opts.pathspec(path);
        }

        let diff = repo.diff_index_to_workdir(None, Some(&mut opts))?;
        Self::diff_to_string(&diff)
    }

    fn diff_to_string(diff: &git2::Diff) -> Result<String, GitError> {
        let mut output = String::new();
        diff.print(git2::DiffFormat::Patch, |_delta, _hunk, line| {
            let prefix = match line.origin() {
                '+' => "+",
                '-' => "-",
                ' ' => " ",
                'H' => "",  // hunk header handled separately
                _ => "",
            };
            if matches!(line.origin(), '+' | '-' | ' ') {
                output.push_str(prefix);
            }
            if let Ok(content) = std::str::from_utf8(line.content()) {
                output.push_str(content);
            }
            true
        })?;
        Ok(output)
    }

    // ── Staging ─────────────────────────────────────────────────────────────

    pub fn stage(&self, repo_path: &str, paths: &[String]) -> Result<(), GitError> {
        let repo = Repository::open(repo_path)?;
        let mut index = repo.index()?;
        for path in paths {
            index.add_path(std::path::Path::new(path))?;
        }
        index.write()?;
        Ok(())
    }

    pub fn unstage(&self, repo_path: &str, paths: &[String]) -> Result<(), GitError> {
        let repo = Repository::open(repo_path)?;
        let head = repo.head()?.peel_to_commit()?;
        let head_tree = head.tree()?;
        let mut index = repo.index()?;

        for path in paths {
            // Reset index entry to HEAD state
            if let Ok(entry) = head_tree.get_path(std::path::Path::new(path)) {
                let blob = repo.find_blob(entry.id())?;
                index.add_frombuffer(
                    &git2::IndexEntry {
                        ctime: git2::IndexTime::new(0, 0),
                        mtime: git2::IndexTime::new(0, 0),
                        dev: 0,
                        ino: 0,
                        mode: entry.filemode() as u32,
                        uid: 0,
                        gid: 0,
                        file_size: blob.content().len() as u32,
                        id: entry.id(),
                        flags: 0,
                        flags_extended: 0,
                        path: path.as_bytes().to_vec(),
                    },
                    blob.content(),
                )?;
            } else {
                // File doesn't exist in HEAD — remove from index
                index.remove_path(std::path::Path::new(path))?;
            }
        }
        index.write()?;
        Ok(())
    }

    pub fn stage_all(&self, repo_path: &str) -> Result<(), GitError> {
        let repo = Repository::open(repo_path)?;
        let mut index = repo.index()?;
        index.add_all(["*"].iter(), git2::IndexAddOption::DEFAULT, None)?;
        index.write()?;
        Ok(())
    }

    pub fn unstage_all(&self, repo_path: &str) -> Result<(), GitError> {
        let repo = Repository::open(repo_path)?;
        if let Ok(head) = repo.head() {
            let obj = head.peel(git2::ObjectType::Commit)?;
            repo.reset(&obj, git2::ResetType::Mixed, None)?;
        }
        Ok(())
    }

    pub fn discard_file(&self, repo_path: &str, file_path: &str) -> Result<(), GitError> {
        let repo = Repository::open(repo_path)?;
        let mut checkout = git2::build::CheckoutBuilder::new();
        checkout.path(file_path);
        checkout.force();
        repo.checkout_head(Some(&mut checkout))?;
        Ok(())
    }

    // ── Commit ──────────────────────────────────────────────────────────────

    pub fn commit(&self, repo_path: &str, message: &str) -> Result<(), GitError> {
        let repo = Repository::open(repo_path)?;
        let mut index = repo.index()?;
        let tree_id = index.write_tree()?;
        let tree = repo.find_tree(tree_id)?;
        let sig = repo.signature()?;

        if repo.head().is_err() {
            repo.commit(Some("HEAD"), &sig, &sig, message, &tree, &[])?;
        } else {
            let parent = repo.head()?.peel_to_commit()?;
            repo.commit(Some("HEAD"), &sig, &sig, message, &tree, &[&parent])?;
        }
        Ok(())
    }

    pub fn commit_amend(&self, repo_path: &str, message: &str) -> Result<(), GitError> {
        let repo = Repository::open(repo_path)?;
        let mut index = repo.index()?;
        let tree_id = index.write_tree()?;
        let tree = repo.find_tree(tree_id)?;
        let head_commit = repo.head()?.peel_to_commit()?;
        let sig = repo.signature()?;

        head_commit.amend(
            Some("HEAD"),
            Some(&sig),
            Some(&sig),
            None,
            Some(message),
            Some(&tree),
        )?;
        Ok(())
    }

    // ── Log ─────────────────────────────────────────────────────────────────

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

    pub fn log_parsed(&self, repo_path: &str, limit: u32) -> Result<Vec<CommitInfo>, GitError> {
        let repo = Repository::open(repo_path)?;
        let mut revwalk = repo.revwalk()?;
        revwalk.push_head()?;
        revwalk.set_sorting(Sort::TIME)?;

        let mut commits = Vec::new();
        let mut count = 0;

        for oid in revwalk {
            if count >= limit {
                break;
            }
            let oid = oid?;
            let commit = repo.find_commit(oid)?;
            let hash = oid.to_string();
            let short_hash = hash[..8].to_string();

            let parents: Vec<String> = commit
                .parent_ids()
                .map(|id| id.to_string()[..8].to_string())
                .collect();

            commits.push(CommitInfo {
                hash,
                short_hash,
                author: commit.author().name().unwrap_or("unknown").to_string(),
                email: commit.author().email().unwrap_or("").to_string(),
                date: commit.time().seconds() * 1000,
                message: commit.message().unwrap_or("").to_string(),
                parents,
                refs: Vec::new(), // TODO: resolve refs
            });
            count += 1;
        }
        Ok(commits)
    }

    // ── Branches ────────────────────────────────────────────────────────────

    pub fn current_branch(&self, repo_path: &str) -> Result<String, GitError> {
        let repo = Repository::open(repo_path)?;
        let head = repo.head()?;
        let branch_name = head.shorthand().unwrap_or("HEAD").to_string();
        Ok(branch_name)
    }

    pub fn branches(&self, repo_path: &str) -> Result<Vec<BranchInfo>, GitError> {
        let repo = Repository::open(repo_path)?;
        let mut result = Vec::new();

        let head_ref = repo.head().ok();
        let current_name = head_ref.as_ref().and_then(|h| h.shorthand().map(|s| s.to_string()));

        // Local branches
        for branch in repo.branches(Some(BranchType::Local))? {
            let (branch, _) = branch?;
            let name = branch.name()?.unwrap_or("?").to_string();
            let is_current = current_name.as_deref() == Some(&name);

            let upstream = branch
                .upstream()
                .ok()
                .and_then(|u| u.name().ok().flatten().map(|s| s.to_string()));

            let (last_hash, last_msg) = branch
                .get()
                .peel_to_commit()
                .ok()
                .map(|c| (
                    Some(c.id().to_string()[..8].to_string()),
                    Some(c.summary().unwrap_or("").to_string()),
                ))
                .unwrap_or((None, None));

            result.push(BranchInfo {
                name,
                is_current,
                is_remote: false,
                upstream,
                last_commit_hash: last_hash,
                last_commit_message: last_msg,
            });
        }

        // Remote branches
        for branch in repo.branches(Some(BranchType::Remote))? {
            let (branch, _) = branch?;
            let name = branch.name()?.unwrap_or("?").to_string();

            let (last_hash, last_msg) = branch
                .get()
                .peel_to_commit()
                .ok()
                .map(|c| (
                    Some(c.id().to_string()[..8].to_string()),
                    Some(c.summary().unwrap_or("").to_string()),
                ))
                .unwrap_or((None, None));

            result.push(BranchInfo {
                name,
                is_current: false,
                is_remote: true,
                upstream: None,
                last_commit_hash: last_hash,
                last_commit_message: last_msg,
            });
        }

        Ok(result)
    }

    pub fn create_branch(&self, repo_path: &str, name: &str, start_point: Option<&str>) -> Result<(), GitError> {
        let repo = Repository::open(repo_path)?;
        let commit = if let Some(rev) = start_point {
            let obj = repo.revparse_single(rev)?;
            obj.peel_to_commit()?
        } else {
            repo.head()?.peel_to_commit()?
        };
        repo.branch(name, &commit, false)?;
        Ok(())
    }

    pub fn switch_branch(&self, repo_path: &str, name: &str) -> Result<(), GitError> {
        let repo = Repository::open(repo_path)?;
        let refname = format!("refs/heads/{}", name);
        let obj = repo.revparse_single(&refname)?;
        repo.checkout_tree(&obj, None)?;
        repo.set_head(&refname)?;
        Ok(())
    }

    pub fn delete_branch(&self, repo_path: &str, name: &str, force: bool) -> Result<(), GitError> {
        let repo = Repository::open(repo_path)?;
        let mut branch = repo.find_branch(name, BranchType::Local)?;
        if force {
            branch.delete()?;
        } else {
            if branch.is_head() {
                return Err(GitError::NotRepo("Cannot delete current branch".to_string()));
            }
            branch.delete()?;
        }
        Ok(())
    }

    pub fn merge_branch(&self, repo_path: &str, name: &str) -> Result<String, GitError> {
        let repo = Repository::open(repo_path)?;
        let branch_ref = format!("refs/heads/{}", name);
        let reference = repo.find_reference(&branch_ref)?;
        let annotated = repo.reference_to_annotated_commit(&reference)?;

        let (analysis, _) = repo.merge_analysis(&[&annotated])?;

        if analysis.is_up_to_date() {
            return Ok("Already up-to-date".to_string());
        }

        if analysis.is_fast_forward() {
            let target = reference.target().ok_or_else(|| {
                GitError::NotRepo("No target for branch ref".to_string())
            })?;
            let mut head_ref = repo.head()?;
            head_ref.set_target(target, &format!("Fast-forward merge {}", name))?;
            repo.checkout_head(Some(git2::build::CheckoutBuilder::new().force()))?;
            return Ok(format!("Fast-forward to {}", name));
        }

        // Normal merge
        repo.merge(&[&annotated], None, None)?;
        Ok(format!("Merged {} (resolve conflicts if any)", name))
    }

    // ── Stash ───────────────────────────────────────────────────────────────

    pub fn stash_list(&self, repo_path: &str) -> Result<Vec<StashEntry>, GitError> {
        let mut repo = Repository::open(repo_path)?;
        let mut entries = Vec::new();

        repo.stash_foreach(|index, message, _oid| {
            entries.push(StashEntry {
                index,
                message: message.to_string(),
                date: 0, // git2 doesn't expose stash date directly
                branch: String::new(),
            });
            true
        })?;

        Ok(entries)
    }

    pub fn stash_push(&self, repo_path: &str, message: Option<&str>, include_untracked: bool) -> Result<(), GitError> {
        let mut repo = Repository::open(repo_path)?;
        let sig = repo.signature()?;
        let mut flags = git2::StashFlags::DEFAULT;
        if include_untracked {
            flags |= git2::StashFlags::INCLUDE_UNTRACKED;
        }
        repo.stash_save(&sig, message.unwrap_or("WIP"), Some(flags))?;
        Ok(())
    }

    pub fn stash_apply(&self, repo_path: &str, index: usize) -> Result<(), GitError> {
        let mut repo = Repository::open(repo_path)?;
        repo.stash_apply(index, None)?;
        Ok(())
    }

    pub fn stash_drop(&self, repo_path: &str, index: usize) -> Result<(), GitError> {
        let mut repo = Repository::open(repo_path)?;
        repo.stash_drop(index)?;
        Ok(())
    }

    pub fn stash_pop(&self, repo_path: &str, index: usize) -> Result<(), GitError> {
        let mut repo = Repository::open(repo_path)?;
        repo.stash_pop(index, None)?;
        Ok(())
    }

    // ── Blame ───────────────────────────────────────────────────────────────

    pub fn blame(&self, repo_path: &str, file_path: &str) -> Result<Vec<BlameLine>, GitError> {
        let repo = Repository::open(repo_path)?;
        let blame = repo.blame_file(std::path::Path::new(file_path), None)?;
        let mut lines = Vec::new();

        // Read file content for line text
        let full_path = std::path::Path::new(repo_path).join(file_path);
        let content = std::fs::read_to_string(&full_path).unwrap_or_default();
        let file_lines: Vec<&str> = content.lines().collect();

        for (i, file_line) in file_lines.iter().enumerate() {
            let line_num = i + 1;
            if let Some(hunk) = blame.get_line(line_num) {
                lines.push(BlameLine {
                    line_num,
                    hash: hunk.final_commit_id().to_string()[..8].to_string(),
                    author: hunk
                        .final_signature()
                        .name()
                        .unwrap_or("unknown")
                        .to_string(),
                    date: hunk.final_signature().when().seconds() * 1000,
                    content: file_line.to_string(),
                });
            }
        }

        Ok(lines)
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

        // Stage and commit
        ops.stage_all(temp_dir.to_str().unwrap()).unwrap();
        ops.commit(temp_dir.to_str().unwrap(), "Initial test commit").unwrap();

        let branch = ops.current_branch(temp_dir.to_str().unwrap()).unwrap();
        assert!(!branch.is_empty());

        let log = ops.log(temp_dir.to_str().unwrap(), 5).unwrap();
        assert!(log.contains("Initial test commit"));

        let parsed = ops.log_parsed(temp_dir.to_str().unwrap(), 5).unwrap();
        assert_eq!(parsed.len(), 1);
        assert!(parsed[0].message.contains("Initial test commit"));

        let branches = ops.branches(temp_dir.to_str().unwrap()).unwrap();
        assert!(!branches.is_empty());

        let _ = std::fs::remove_dir_all(&temp_dir);
    }
}
