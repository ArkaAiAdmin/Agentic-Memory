import React, { useState } from "react";
import { useAppStore } from "../../stores/appStore";
import { GitChangesView } from "./GitChangesView";
import { GitLogView } from "./GitLogView";
import { GitBranchesView } from "./GitBranchesView";
import { GitStashView } from "./GitStashView";

type GitTab = "changes" | "log" | "branches" | "stash";

const TABS: { key: GitTab; label: string }[] = [
  { key: "changes", label: "Changes" },
  { key: "log", label: "Log" },
  { key: "branches", label: "Branches" },
  { key: "stash", label: "Stash" },
];

export function GitPanel() {
  const { activeProject } = useAppStore();
  const [tab, setTab] = useState<GitTab>("changes");
  const repoPath = activeProject ?? "";

  if (!repoPath) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "var(--text-tertiary)", fontSize: 12 }}>
        Open a project to use Git
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", fontSize: 12 }}>
      {/* Tab bar */}
      <div style={{
        display: "flex", borderBottom: "1px solid var(--border-default)",
        padding: "0 8px", background: "var(--bg-secondary)",
      }}>
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            style={{
              padding: "8px 12px", border: "none", background: "transparent",
              color: tab === t.key ? "var(--accent)" : "var(--text-secondary)",
              fontSize: 11, fontWeight: tab === t.key ? 600 : 400, cursor: "pointer",
              borderBottom: tab === t.key ? "2px solid var(--accent)" : "2px solid transparent",
            }}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Active view */}
      <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
        {tab === "changes" && <GitChangesView repoPath={repoPath} />}
        {tab === "log" && <GitLogView repoPath={repoPath} />}
        {tab === "branches" && <GitBranchesView repoPath={repoPath} />}
        {tab === "stash" && <GitStashView repoPath={repoPath} />}
      </div>
    </div>
  );
}
