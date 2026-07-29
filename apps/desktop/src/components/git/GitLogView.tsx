import React, { useState, useEffect, useCallback } from "react";
import { git, GitCommitInfo } from "../../ipc/client";

const NOW = Date.now();

export function GitLogView({ repoPath }: { repoPath: string }) {
  const [commits, setCommits] = useState<GitCommitInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [selectedHash, setSelectedHash] = useState<string | null>(null);
  const [limit, setLimit] = useState(100);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const result = await git.logParsed(repoPath, limit);
      setCommits(result || []);
    } catch (err) {
      console.error("Git log failed:", err);
    } finally {
      setLoading(false);
    }
  }, [repoPath, limit]);

  useEffect(() => {
    // refresh() manages its own loading state — standard async init pattern
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
  }, [refresh]);

  const filtered = search.trim()
    ? commits.filter(
        (c) =>
          c.message.toLowerCase().includes(search.toLowerCase()) ||
          c.author.toLowerCase().includes(search.toLowerCase()) ||
          c.hash.startsWith(search)
      )
    : commits;

  const formatDate = (ts: number) => {
    const d = new Date(ts);
    const diff = NOW - ts;
    if (diff < 60000) return "just now";
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
    if (diff < 604800000) return `${Math.floor(diff / 86400000)}d ago`;
    return d.toLocaleDateString();
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* Search */}
      <div style={{ padding: "8px 14px", borderBottom: "1px solid var(--border-default)" }}>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search commits (message, author, hash)..."
            aria-label="Search commits"
            style={{
            width: "100%", padding: "6px 10px", borderRadius: "var(--radius-sm)",
            border: "1px solid var(--border-default)", background: "var(--bg-tertiary)",
            color: "var(--text-primary)", fontSize: 11, outline: "none", boxSizing: "border-box",
          }}
        />
      </div>

      {/* Commit list */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {loading && commits.length === 0 && (
          <div style={{ padding: 20, textAlign: "center", color: "var(--text-tertiary)" }}>Loading...</div>
        )}
        {filtered.map((c, _i) => {
          const isMultiParent = c.parents.length > 1;
          return (
            <div
              key={c.hash}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setSelectedHash(selectedHash === c.hash ? null : c.hash);
                }
              }}
              onClick={() => setSelectedHash(selectedHash === c.hash ? null : c.hash)}
              style={{
                padding: "6px 14px", cursor: "pointer",
                borderBottom: "1px solid var(--border-subtle)",
                background: selectedHash === c.hash ? "var(--bg-hover)" : "transparent",
              }}
              onMouseEnter={(e) => { if (selectedHash !== c.hash) e.currentTarget.style.background = "var(--bg-hover)"; }}
              onMouseLeave={(e) => { if (selectedHash !== c.hash) e.currentTarget.style.background = "transparent"; }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                {/* Graph indicator */}
                <span style={{
                  width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                  background: isMultiParent ? "var(--warning)" : "var(--accent)",
                  border: isMultiParent ? "2px solid var(--warning)" : "none",
                }} />
                {/* Hash */}
                <code style={{ fontSize: 10, color: "var(--text-tertiary)", fontFamily: "var(--font-mono)", flexShrink: 0 }}>
                  {c.shortHash}
                </code>
                {/* Message */}
                <span style={{
                  flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                  color: "var(--text-primary)", fontSize: 12,
                }}>
                  {c.message.split("\n")[0]}
                </span>
                {/* Date */}
                <span style={{ fontSize: 10, color: "var(--text-tertiary)", flexShrink: 0 }}>
                  {formatDate(c.date)}
                </span>
              </div>
              {/* Expanded detail */}
              {selectedHash === c.hash && (
                <div style={{ marginTop: 6, paddingLeft: 16, fontSize: 11 }}>
                  <div style={{ color: "var(--text-secondary)" }}>
                    <strong>Author:</strong> {c.author} &lt;{c.email}&gt;
                  </div>
                  <div style={{ color: "var(--text-secondary)", marginTop: 2 }}>
                    <strong>Date:</strong> {new Date(c.date).toLocaleString()}
                  </div>
                  {c.parents.length > 0 && (
                    <div style={{ color: "var(--text-tertiary)", marginTop: 2 }}>
                      <strong>Parents:</strong> {c.parents.join(", ")}
                    </div>
                  )}
                  {c.message.includes("\n") && (
                    <pre style={{
                      marginTop: 4, fontSize: 11, color: "var(--text-secondary)",
                      whiteSpace: "pre-wrap", fontFamily: "var(--font-mono)",
                    }}>
                      {c.message}
                    </pre>
                  )}
                </div>
              )}
            </div>
          );
        })}
        {!loading && filtered.length === 0 && (
          <div style={{ padding: 20, textAlign: "center", color: "var(--text-tertiary)" }}>
            {search ? "No commits match filter" : "No commits"}
          </div>
        )}
        {filtered.length >= limit && (
          <button
            onClick={() => setLimit((l) => l + 100)}
            style={{
              width: "100%", padding: "8px", border: "none",
              background: "var(--bg-tertiary)", color: "var(--accent)",
              fontSize: 11, cursor: "pointer",
            }}
          >
            Load more...
          </button>
        )}
      </div>
    </div>
  );
}
