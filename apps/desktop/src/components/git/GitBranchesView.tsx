import React, { useState, useEffect, useCallback } from "react";
import { git, GitBranchInfo } from "../../ipc/client";

export function GitBranchesView({ repoPath }: { repoPath: string }) {
  const [branches, setBranches] = useState<GitBranchInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [newBranchName, setNewBranchName] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [filter, setFilter] = useState<"local" | "remote" | "all">("all");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const result = await git.branches(repoPath);
      setBranches(result || []);
    } catch (err) {
      console.error("Git branches failed:", err);
    } finally {
      setLoading(false);
    }
  }, [repoPath]);

  useEffect(() => {
    // refresh() manages its own loading state — standard async init pattern
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
  }, [refresh]);

  const handleCreate = async () => {
    if (!newBranchName.trim()) return;
    try {
      await git.createBranch(repoPath, newBranchName.trim());
      setNewBranchName("");
      setShowCreate(false);
      await refresh();
    } catch (err) {
      console.error("Create branch failed:", err);
    }
  };

  const handleSwitch = async (name: string) => {
    try {
      await git.switchBranch(repoPath, name);
      await refresh();
    } catch (err) {
      console.error("Switch branch failed:", err);
    }
  };

  const handleDelete = async (name: string, force: boolean = false) => {
    if (!confirm(`Delete branch "${name}"${force ? " (force)" : ""}?`)) return;
    try {
      await git.deleteBranch(repoPath, name, force);
      await refresh();
    } catch (err) {
      console.error("Delete branch failed:", err);
      if (!force && confirm(`Branch may not be fully merged. Force delete "${name}"?`)) {
        await git.deleteBranch(repoPath, name, true);
        await refresh();
      }
    }
  };

  const handleMerge = async (name: string) => {
    try {
      const result = await git.mergeBranch(repoPath, name);
      if (result?.message) alert(result.message);
      await refresh();
    } catch (err) {
      console.error("Merge failed:", err);
    }
  };

  const filtered = branches.filter((b) => {
    if (filter === "local") return !b.isRemote;
    if (filter === "remote") return b.isRemote;
    return true;
  });

  const localBranches = filtered.filter((b) => !b.isRemote);
  const remoteBranches = filtered.filter((b) => b.isRemote);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Header with filter + create */}
      <div style={{
        padding: "8px 14px", borderBottom: "1px solid var(--border-default)",
        display: "flex", alignItems: "center", justifyContent: "space-between",
      }}>
        <div style={{ display: "flex", gap: 4 }}>
          {(["all", "local", "remote"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              style={{
                padding: "3px 8px", borderRadius: "var(--radius-sm)", border: "none",
                background: filter === f ? "var(--accent)" : "var(--bg-tertiary)",
                color: filter === f ? "var(--accent-text)" : "var(--text-secondary)",
                fontSize: 10, cursor: "pointer", textTransform: "capitalize",
              }}
            >
              {f}
            </button>
          ))}
        </div>
        <button
          onClick={() => setShowCreate(!showCreate)}
          style={{
            padding: "3px 8px", borderRadius: "var(--radius-sm)",
            border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
            color: "var(--accent)", fontSize: 10, cursor: "pointer",
          }}
        >
          + New
        </button>
      </div>

      {/* Create branch input */}
      {showCreate && (
        <div style={{ padding: "8px 14px", borderBottom: "1px solid var(--border-default)", display: "flex", gap: 6 }}>
          <input
            value={newBranchName}
            onChange={(e) => setNewBranchName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleCreate()}
            placeholder="New branch name..."
            autoFocus
            style={{
              flex: 1, padding: "6px 10px", borderRadius: "var(--radius-sm)",
              border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
              color: "var(--text-primary)", fontSize: 11, outline: "none",
            }}
          />
          <button onClick={handleCreate} style={{
            padding: "6px 12px", borderRadius: "var(--radius-sm)", border: "none",
            background: "var(--accent)", color: "var(--accent-text)", fontSize: 11, cursor: "pointer",
          }}>
            Create
          </button>
        </div>
      )}

      {/* Branch list */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {localBranches.length > 0 && (
          <BranchSection
            label="Local"
            branches={localBranches}
            onSwitch={handleSwitch}
            onDelete={handleDelete}
            onMerge={handleMerge}
          />
        )}
        {remoteBranches.length > 0 && (
          <BranchSection
            label="Remote"
            branches={remoteBranches}
            onSwitch={handleSwitch}
            onDelete={handleDelete}
            onMerge={handleMerge}
          />
        )}
        {!loading && filtered.length === 0 && (
          <div style={{ padding: 20, textAlign: "center", color: "var(--text-tertiary)" }}>No branches</div>
        )}
      </div>
    </div>
  );
}

function BranchSection({
  label, branches, onSwitch, onDelete, onMerge,
}: {
  label: string;
  branches: GitBranchInfo[];
  onSwitch: (name: string) => void;
  onDelete: (name: string) => void;
  onMerge: (name: string) => void;
}) {
  return (
    <div>
      <div style={{
        padding: "6px 14px", fontSize: 10, fontWeight: 600,
        color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: 0.5,
        borderBottom: "1px solid var(--border-subtle)",
      }}>
        {label} ({branches.length})
      </div>
      {branches.map((b) => (
        <div
          key={b.name}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              if (!b.isRemote && !b.isCurrent) onSwitch(b.name);
            }
          }}
          style={{
            padding: "6px 14px", display: "flex", alignItems: "center", gap: 8,
            borderBottom: "1px solid var(--border-subtle)",
          }}
          onMouseEnter={(e) => e.currentTarget.style.background = "var(--bg-hover)"}
          onMouseLeave={(e) => e.currentTarget.style.background = "transparent"}
        >
          {/* Current indicator */}
          <span style={{
            width: 6, height: 6, borderRadius: "50%", flexShrink: 0,
            background: b.isCurrent ? "var(--success)" : "transparent",
            border: b.isCurrent ? "none" : "1px solid var(--border-default)",
          }} />
          {/* Branch name */}
          <span style={{
            flex: 1, fontSize: 12, color: "var(--text-primary)",
            fontWeight: b.isCurrent ? 600 : 400,
            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>
            {b.name}
          </span>
          {/* Upstream */}
          {b.upstream && (
            <span style={{ fontSize: 9, color: "var(--text-tertiary)" }}>
              → {b.upstream}
            </span>
          )}
          {/* Last commit */}
          {b.lastCommitHash && (
            <code style={{ fontSize: 9, color: "var(--text-tertiary)", fontFamily: "var(--font-mono)" }}>
              {b.lastCommitHash}
            </code>
          )}
          {/* Actions */}
          {!b.isCurrent && !b.isRemote && (
            <div style={{ display: "flex", gap: 2 }}>
              <button
                onClick={() => onSwitch(b.name)}
                title="Switch"
                aria-label="Switch to branch"
                style={actionBtnStyle}
              >
                ↗
              </button>
              <button
                onClick={() => onMerge(b.name)}
                title="Merge into current"
                aria-label="Merge branch"
                style={actionBtnStyle}
              >
                ⑂
              </button>
              <button
                onClick={() => onDelete(b.name)}
                title="Delete"
                aria-label="Delete branch"
                style={{ ...actionBtnStyle, color: "var(--error)" }}
              >
                ×
              </button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

const actionBtnStyle: React.CSSProperties = {
  width: 20, height: 20, borderRadius: "var(--radius-sm)",
  border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
  color: "var(--accent)", fontSize: 11, cursor: "pointer",
  display: "flex", alignItems: "center", justifyContent: "center",
};
