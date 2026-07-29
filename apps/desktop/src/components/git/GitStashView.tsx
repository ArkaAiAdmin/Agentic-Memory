import React, { useState, useEffect, useCallback } from "react";
import { git, GitStashEntry } from "../../ipc/client";

export function GitStashView({ repoPath }: { repoPath: string }) {
  const [stashes, setStashes] = useState<GitStashEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [showPush, setShowPush] = useState(false);
  const [stashMsg, setStashMsg] = useState("");
  const [includeUntracked, setIncludeUntracked] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const result = await git.stashList(repoPath);
      setStashes(result || []);
    } catch (err) {
      console.error("Stash list failed:", err);
    } finally {
      setLoading(false);
    }
  }, [repoPath]);

  useEffect(() => {
    // refresh() manages its own loading state — standard async init pattern
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
  }, [refresh]);

  const handlePush = async () => {
    try {
      await git.stashPush(repoPath, stashMsg || undefined, includeUntracked);
      setStashMsg("");
      setShowPush(false);
      await refresh();
    } catch (err) {
      console.error("Stash push failed:", err);
    }
  };

  const handleApply = async (index: number) => {
    try {
      await git.stashApply(repoPath, index);
      await refresh();
    } catch (err) {
      console.error("Stash apply failed:", err);
    }
  };

  const handlePop = async (index: number) => {
    try {
      await git.stashPop(repoPath, index);
      await refresh();
    } catch (err) {
      console.error("Stash pop failed:", err);
    }
  };

  const handleDrop = async (index: number) => {
    if (!confirm(`Drop stash@{${index}}? This cannot be undone.`)) return;
    try {
      await git.stashDrop(repoPath, index);
      await refresh();
    } catch (err) {
      console.error("Stash drop failed:", err);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Header */}
      <div style={{
        padding: "8px 14px", borderBottom: "1px solid var(--border-default)",
        display: "flex", alignItems: "center", justifyContent: "space-between",
      }}>
        <span style={{ fontSize: 11, color: "var(--text-secondary)", fontWeight: 600 }}>
          {stashes.length} stash{stashes.length !== 1 ? "es" : ""}
        </span>
        <button
          onClick={() => setShowPush(!showPush)}
          style={{
            padding: "3px 8px", borderRadius: "var(--radius-sm)",
            border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
            color: "var(--accent)", fontSize: 10, cursor: "pointer",
          }}
        >
          + Stash
        </button>
      </div>

      {/* Push stash form */}
      {showPush && (
        <div style={{ padding: "8px 14px", borderBottom: "1px solid var(--border-default)" }}>
          <input
            value={stashMsg}
            onChange={(e) => setStashMsg(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handlePush()}
            placeholder="Stash message (optional)..."
            autoFocus
            style={{
              width: "100%", padding: "6px 10px", borderRadius: "var(--radius-sm)",
              border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
              color: "var(--text-primary)", fontSize: 11, outline: "none",
              boxSizing: "border-box", marginBottom: 6,
            }}
          />
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <label style={{ fontSize: 11, color: "var(--text-tertiary)", display: "flex", alignItems: "center", gap: 4, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={includeUntracked}
                onChange={(e) => setIncludeUntracked(e.target.checked)}
                style={{ margin: 0 }}
              />
              Include untracked
            </label>
            <button onClick={handlePush} style={{
              padding: "5px 12px", borderRadius: "var(--radius-sm)", border: "none",
              background: "var(--accent)", color: "var(--accent-text)", fontSize: 11, cursor: "pointer",
            }}>
              Stash
            </button>
          </div>
        </div>
      )}

      {/* Stash list */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {stashes.length === 0 && !loading && (
          <div style={{ padding: 20, textAlign: "center", color: "var(--text-tertiary)" }}>
            No stashes
          </div>
        )}
        {stashes.map((s) => (
          <div
            key={s.index}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
              }
            }}
            style={{
              padding: "8px 14px", borderBottom: "1px solid var(--border-subtle)",
              display: "flex", alignItems: "center", gap: 8,
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = "var(--bg-hover)"}
            onMouseLeave={(e) => e.currentTarget.style.background = "transparent"}
          >
            {/* Index badge */}
            <span style={{
              fontSize: 9, fontFamily: "var(--font-mono)", color: "var(--text-tertiary)",
              background: "var(--bg-tertiary)", padding: "2px 4px", borderRadius: 3,
              flexShrink: 0,
            }}>
              @{"{" + s.index + "}"}
            </span>
            {/* Message */}
            <span style={{
              flex: 1, fontSize: 12, color: "var(--text-primary)",
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
            }}>
              {s.message || "(no message)"}
            </span>
            {/* Actions */}
            <div style={{ display: "flex", gap: 4, flexShrink: 0 }}>
              <button
                onClick={() => handleApply(s.index)}
                title="Apply (keep stash)"
                aria-label="Apply stash"
                style={stashBtnStyle}
              >
                Apply
              </button>
              <button
                onClick={() => handlePop(s.index)}
                title="Pop (apply and remove)"
                aria-label="Pop stash"
                style={stashBtnStyle}
              >
                Pop
              </button>
              <button
                onClick={() => handleDrop(s.index)}
                title="Drop (delete)"
                aria-label="Drop stash"
                style={{ ...stashBtnStyle, color: "var(--error)", borderColor: "var(--error)" }}
              >
                Drop
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

const stashBtnStyle: React.CSSProperties = {
  padding: "3px 6px", borderRadius: "var(--radius-sm)",
  border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
  color: "var(--accent)", fontSize: 9, cursor: "pointer",
};
