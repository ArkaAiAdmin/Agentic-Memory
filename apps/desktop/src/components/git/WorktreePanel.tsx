/**
 * WorktreePanel — Git worktree management.
 *
 * List, create, switch, and remove worktrees.
 */

import React, { useState, useEffect, useCallback } from "react";
import { useAppStore } from "../../stores/appStore";
import {
  listWorktrees,
  createWorktree,
  removeWorktree,
  pruneWorktrees,
  onWorktreeChange,
  type Worktree,
} from "../../services/worktrees";

export function WorktreePanel() {
  const { activeProject } = useAppStore();
  const [worktrees, setWorktrees] = useState<Worktree[]>([]);
  const [loading, setLoading] = useState(false);
  const [newBranch, setNewBranch] = useState("");
  const [newPath, setNewPath] = useState("");
  const [showCreate, setShowCreate] = useState(false);

  const repoPath = activeProject ?? "";

  const refresh = useCallback(async () => {
    if (!repoPath) return;
    setLoading(true);
    await listWorktrees(repoPath);
    setLoading(false);
  }, [repoPath]);

  useEffect(() => {
    // refresh() manages its own loading state — standard async init pattern
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    return onWorktreeChange(setWorktrees);
  }, [refresh]);

  const handleCreate = useCallback(async () => {
    if (!repoPath || !newBranch.trim() || !newPath.trim()) return;
    setLoading(true);
    const ok = await createWorktree(repoPath, newBranch.trim(), newPath.trim());
    setLoading(false);
    if (ok) {
      setNewBranch("");
      setNewPath("");
      setShowCreate(false);
    }
  }, [repoPath, newBranch, newPath]);

  const handleRemove = useCallback(async (path: string) => {
    if (!repoPath) return;
    setLoading(true);
    await removeWorktree(repoPath, path);
    setLoading(false);
  }, [repoPath]);

  const handlePrune = useCallback(async () => {
    if (!repoPath) return;
    setLoading(true);
    await pruneWorktrees(repoPath);
    setLoading(false);
  }, [repoPath]);

  return (
    <div style={{ padding: 16, height: "100%", overflow: "auto" }}>
      <div style={{
        display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16,
      }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-secondary)" }}>
          Worktrees
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          <button onClick={handlePrune} disabled={loading} style={actionBtn}>Prune</button>
          <button onClick={() => setShowCreate(!showCreate)} style={actionBtn}>
            {showCreate ? "Cancel" : "+ New"}
          </button>
        </div>
      </div>

      {/* Create form */}
      {showCreate && (
        <div style={{
          padding: 12, marginBottom: 12, borderRadius: "var(--radius-md)",
          border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
        }}>
          <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: 8 }}>Create worktree</div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <input
              value={newBranch}
              onChange={(e) => setNewBranch(e.target.value)}
              placeholder="Branch name (e.g. feature/my-feature)"
              style={inputStyle}
            />
            <input
              value={newPath}
              onChange={(e) => setNewPath(e.target.value)}
              placeholder="Path (e.g. ../my-feature)"
              style={inputStyle}
            />
            <button onClick={handleCreate} disabled={!newBranch.trim() || !newPath.trim()} style={actionBtn}>
              Create Worktree
            </button>
          </div>
        </div>
      )}

      {/* Worktree list */}
      {worktrees.length === 0 && !loading && (
        <div style={{ textAlign: "center", padding: 20, color: "var(--text-tertiary)", fontSize: 12 }}>
          No worktrees found
        </div>
      )}

      {worktrees.map((wt) => (
        <div key={wt.path} style={{
          padding: "10px 12px", marginBottom: 6, borderRadius: "var(--radius-md)",
          border: `1px solid ${wt.isCurrent ? "var(--accent)" : "var(--border-default)"}`,
          background: wt.isCurrent ? "var(--accent-muted)" : "var(--bg-tertiary)",
        }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <div>
              <div style={{ fontSize: 12, fontWeight: 500, color: "var(--text-primary)" }}>
                {wt.branch}
                {wt.isCurrent && (
                  <span style={{
                    marginLeft: 6, fontSize: 9, padding: "1px 5px", borderRadius: "var(--radius-xs)",
                    background: "var(--accent)", color: "var(--accent-text)", fontWeight: 600,
                  }}>CURRENT</span>
                )}
              </div>
              <div style={{ fontSize: 10, color: "var(--text-tertiary)", marginTop: 2, fontFamily: "var(--font-mono)" }}>
                {wt.path}
              </div>
            </div>
            {!wt.isCurrent && (
              <button onClick={() => handleRemove(wt.path)} disabled={loading} style={actionBtn}>
                Remove
              </button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

const actionBtn: React.CSSProperties = {
  padding: "4px 10px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
  background: "transparent", color: "var(--text-secondary)", fontSize: 11, fontWeight: 500, cursor: "pointer",
};
const inputStyle: React.CSSProperties = {
  padding: "8px 12px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
  background: "var(--bg-primary)", color: "var(--text-primary)", fontSize: 12,
};
