import React, { useState, useEffect, useCallback } from "react";
import { git } from "../../ipc/client";
import { DiffViewer } from "../diff/DiffViewer";

interface GitFile {
  path: string;
  status: string;
  staged: boolean;
}

function parseStatus(raw: string): GitFile[] {
  return raw
    .split("\n")
    .filter((l) => l.length >= 3 && !l.startsWith("##"))
    .map((line) => {
      const idx = line[0];
      const wt = line[1];
      const path = line.slice(3);
      const staged = idx !== " " && idx !== "?";
      let status = "modified";
      if (idx === "A" || (wt === "?" && idx === "?")) status = idx === "A" ? "added" : "untracked";
      else if (idx === "D" || wt === "D") status = "deleted";
      else if (idx === "R" || wt === "R") status = "renamed";
      return { path, status, staged };
    });
}

export function GitChangesView({ repoPath }: { repoPath: string }) {
  const [files, setFiles] = useState<GitFile[]>([]);
  const [branch, setBranch] = useState("");
  const [commitMsg, setCommitMsg] = useState("");
  const [amend, setAmend] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedFile, setExpandedFile] = useState<string | null>(null);
  const [diffContent, setDiffContent] = useState("");
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    setDiffError(null);
    try {
      const [status, br] = await Promise.all([
        git.status(repoPath),
        git.branch(repoPath),
      ]);
      if (!status || status.startsWith("##") && !status.includes("\n")) {
        setFiles([]);
      } else {
        setFiles(parseStatus(status));
      }
      setBranch(br.trim());
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error("Git refresh failed:", msg);
      setError(msg);
      setFiles([]);
    } finally {
      setLoading(false);
    }
  }, [repoPath]);

  useEffect(() => {
    // refresh() manages its own loading state — standard async init pattern
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
  }, [refresh]);

  const handleStage = async (paths: string[]) => {
    await git.stage(repoPath, paths);
    await refresh();
  };

  const handleUnstage = async (paths: string[]) => {
    await git.unstage(repoPath, paths);
    await refresh();
  };

  const handleStageAll = async () => {
    await git.stageAll(repoPath);
    await refresh();
  };

  const handleUnstageAll = async () => {
    await git.unstageAll(repoPath);
    await refresh();
  };

  const handleDiscard = async (path: string) => {
    if (!confirm(`Discard changes to ${path}?`)) return;
    await git.discardFile(repoPath, path);
    await refresh();
  };

  const handleCommit = async () => {
    if (!commitMsg.trim()) return;
    setLoading(true);
    try {
      await git.commit(repoPath, commitMsg.trim(), amend);
      setCommitMsg("");
      setAmend(false);
      await refresh();
    } catch (err) {
      console.error("Commit failed:", err);
    } finally {
      setLoading(false);
    }
  };

  const handlePush = async () => {
    setLoading(true);
    try { await git.push(repoPath); } catch (e) { console.error(e); }
    setLoading(false);
  };

  const handlePull = async () => {
    setLoading(true);
    try { await git.pull(repoPath); await refresh(); } catch (e) { console.error(e); }
    setLoading(false);
  };

  const toggleDiff = async (file: GitFile) => {
    if (expandedFile === file.path) {
      setExpandedFile(null);
      setDiffContent("");
      setDiffError(null);
      return;
    }
    setDiffLoading(true);
    setDiffError(null);
    try {
      const d = file.staged
        ? await git.diffStaged(repoPath, file.path)
        : await git.diffUnstaged(repoPath, file.path);
      setDiffContent(d || "");
      setExpandedFile(file.path);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error("Git diff failed:", msg);
      setDiffError(msg);
      setDiffContent("");
      setExpandedFile(null);
    } finally {
      setDiffLoading(false);
    }
  };

  const stagedFiles = files.filter((f) => f.staged);
  const unstagedFiles = files.filter((f) => !f.staged);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Branch header + remote actions */}
      <div style={{
        padding: "8px 14px", borderBottom: "1px solid var(--border-default)",
        display: "flex", alignItems: "center", justifyContent: "space-between",
      }}>
        <span style={{ fontWeight: 600, color: "var(--text-primary)", fontSize: 12 }}>
          {branch || "detached"}
        </span>
        <div style={{ display: "flex", gap: 4 }}>
          <ActionBtn label="Pull" onClick={handlePull} disabled={loading} />
          <ActionBtn label="Push" onClick={handlePush} disabled={loading} />
          <ActionBtn label="Refresh" onClick={refresh} disabled={loading} />
        </div>
      </div>

      {/* File lists */}
      <div style={{ flex: 1, overflowY: "auto", overflowX: "hidden", minHeight: 0 }}>
        {error && (
          <div style={{ padding: 16, color: "var(--error)", textAlign: "center", fontSize: 11 }}>
            {error}
          </div>
        )}
        {files.length === 0 && !loading && !error && (
          <div style={{ padding: 20, color: "var(--text-tertiary)", textAlign: "center" }}>
            Working tree clean
          </div>
        )}

        {/* Staged section */}
        {stagedFiles.length > 0 && (
          <FileSection
            label="Staged Changes"
            count={stagedFiles.length}
            files={stagedFiles}
            onHeaderAction={handleUnstageAll}
            headerActionLabel="Unstage All"
            onFileClick={toggleDiff}
            onAction={(f) => handleUnstage([f.path])}
            actionLabel="−"
            expandedFile={expandedFile}
            diffContent={diffContent}
            diffLoading={diffLoading}
            diffError={diffError}
          />
        )}

        {/* Unstaged section */}
        {unstagedFiles.length > 0 && (
          <FileSection
            label="Changes"
            count={unstagedFiles.length}
            files={unstagedFiles}
            onHeaderAction={handleStageAll}
            headerActionLabel="Stage All"
            onFileClick={toggleDiff}
            onAction={(f) => handleStage([f.path])}
            actionLabel="+"
            onSecondaryAction={handleDiscard}
            secondaryActionLabel="Discard"
            expandedFile={expandedFile}
            diffContent={diffContent}
            diffLoading={diffLoading}
            diffError={diffError}
          />
        )}
      </div>

      {/* Commit area */}
      <div style={{ padding: "10px 14px", borderTop: "1px solid var(--border-default)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <span style={{ fontSize: 11, color: "var(--text-tertiary)" }}>
            {stagedFiles.length} file{stagedFiles.length !== 1 ? "s" : ""} staged
          </span>
          <label style={{ fontSize: 11, color: "var(--text-tertiary)", display: "flex", alignItems: "center", gap: 4, cursor: "pointer" }}>
            <input type="checkbox" checked={amend} onChange={(e) => setAmend(e.target.checked)} style={{ margin: 0 }} />
            Amend
          </label>
        </div>
        <textarea
          value={commitMsg}
          onChange={(e) => setCommitMsg(e.target.value)}
          placeholder="Commit message (first line is subject)..."
          aria-label="Commit message"
          rows={3}
          style={{
            width: "100%", padding: "8px 10px", borderRadius: "var(--radius-sm)",
            border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
            color: "var(--text-primary)", fontSize: 12, outline: "none", resize: "vertical",
            fontFamily: "inherit", boxSizing: "border-box",
          }}
        />
        <button
          onClick={handleCommit}
          disabled={loading || !commitMsg.trim() || (stagedFiles.length === 0 && !amend)}
          style={{
            marginTop: 6, width: "100%", padding: "8px", borderRadius: "var(--radius-sm)",
            border: "none", fontSize: 12, fontWeight: 600, cursor: "pointer",
            background: commitMsg.trim() && (stagedFiles.length > 0 || amend) ? "var(--accent)" : "var(--bg-elevated)",
            color: commitMsg.trim() && (stagedFiles.length > 0 || amend) ? "var(--accent-text)" : "var(--text-tertiary)",
          }}
        >
          {amend ? "Amend Commit" : "Commit"}
        </button>
      </div>
    </div>
  );
}

function ActionBtn({ label, onClick, disabled }: { label: string; onClick: () => void; disabled?: boolean }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: "4px 8px", borderRadius: "var(--radius-sm)",
        border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
        color: "var(--text-secondary)", fontSize: 10, cursor: disabled ? "default" : "pointer",
        opacity: disabled ? 0.5 : 1,
      }}
    >
      {label}
    </button>
  );
}

interface FileSectionProps {
  label: string;
  count: number;
  files: GitFile[];
  onHeaderAction: () => void;
  headerActionLabel: string;
  onFileClick: (f: GitFile) => void;
  onAction: (f: GitFile) => void;
  actionLabel: string;
  onSecondaryAction?: (path: string) => void;
  secondaryActionLabel?: string;
  expandedFile: string | null;
  diffContent: string;
  diffLoading: boolean;
  diffError: string | null;
}

function FileSection({
  label, count, files, onHeaderAction, headerActionLabel,
  onFileClick, onAction, actionLabel, onSecondaryAction, secondaryActionLabel,
  expandedFile, diffContent, diffLoading, diffError,
}: FileSectionProps) {
  return (
    <div>
      <div style={{
        padding: "6px 14px", display: "flex", alignItems: "center", justifyContent: "space-between",
        borderBottom: "1px solid var(--border-subtle)",
      }}>
        <span style={{ fontSize: 11, fontWeight: 600, color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: 0.5 }}>
          {label} ({count})
        </span>
        <button
          onClick={onHeaderAction}
          style={{
            padding: "2px 6px", border: "none", background: "transparent",
            color: "var(--accent)", fontSize: 10, cursor: "pointer", fontWeight: 500,
          }}
        >
          {headerActionLabel}
        </button>
      </div>
      {files.map((f) => {
        const statusColor = f.status === "added" || f.status === "untracked" ? "var(--success)"
          : f.status === "deleted" ? "var(--error)" : "var(--warning)";
        return (
          <React.Fragment key={f.path}>
            <div
              onClick={() => onFileClick(f)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onFileClick(f);
                }
              }}
              style={{
                padding: "4px 14px", cursor: "pointer", display: "flex",
                alignItems: "center", gap: 6, fontSize: 12,
                background: expandedFile === f.path ? "var(--bg-hover)" : "transparent",
              }}
              onMouseEnter={(e) => { if (expandedFile !== f.path) e.currentTarget.style.background = "var(--bg-hover)"; }}
              onMouseLeave={(e) => { if (expandedFile !== f.path) e.currentTarget.style.background = "transparent"; }}
            >
              <span style={{
                width: 14, height: 14, borderRadius: 3, background: statusColor,
                display: "flex", alignItems: "center", justifyContent: "center",
                fontSize: 9, fontWeight: 700, color: "var(--bg-primary)", flexShrink: 0,
              }}>
                {f.status[0].toUpperCase()}
              </span>
              <span style={{ flex: 1, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {f.path}
              </span>
              <div style={{ display: "flex", gap: 4 }} onClick={(e) => e.stopPropagation()}>
                {onSecondaryAction && (
                  <button
                    onClick={() => onSecondaryAction(f.path)}
                    title={secondaryActionLabel}
                    style={{ ...miniBtn, color: "var(--error)" }}
                  >
                    ×
                  </button>
                )}
                <button
                  onClick={() => onAction(f)}
                  title={actionLabel === "+" ? "Stage" : "Unstage"}
                  style={{ ...miniBtn, color: "var(--accent)" }}
                >
                  {actionLabel}
                </button>
              </div>
            </div>
            {expandedFile === f.path && (
              <div style={{ maxHeight: 300, overflow: "auto", borderBottom: "1px solid var(--border-subtle)" }}>
                {diffLoading && (
                  <div style={{ padding: 12, color: "var(--text-tertiary)", fontSize: 11 }}>Loading diff…</div>
                )}
                {!diffLoading && diffError && (
                  <div style={{ padding: 12, color: "var(--error)", fontSize: 11 }}>{diffError}</div>
                )}
                {!diffLoading && !diffError && diffContent && (
                  <DiffViewer oldText="" newText="" rawDiff={diffContent} />
                )}
              </div>
            )}
          </React.Fragment>
        );
      })}
    </div>
  );
}

const miniBtn: React.CSSProperties = {
  width: 20, height: 20, borderRadius: "var(--radius-sm)",
  border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
  fontSize: 12, fontWeight: 700, cursor: "pointer", display: "flex",
  alignItems: "center", justifyContent: "center", lineHeight: 1,
};
