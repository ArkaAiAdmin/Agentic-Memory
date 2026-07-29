import React, { useState } from "react";
import { useAppStore } from "../stores/appStore";
import type { Project } from "@ami/shared";

export function ProjectOpenDialog({ onClose }: { onClose: () => void }) {
  const { addProject, setActiveProject } = useAppStore();
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const openPath = async (targetPath: string) => {
    if (!targetPath.trim()) { setError("Please enter a project path"); return; }
    setLoading(true);
    setError(null);
    try {
      const { fs } = await import("../ipc/client");
      const entries = await fs.listDir(targetPath.trim());
      const parts = targetPath.trim().replace(/\/$/, "").split("/");
      const name = parts[parts.length - 1] || "Untitled";
      const project: Project = {
        root: targetPath.trim(),
        name,
        files: entries.map((e) => ({ path: `${targetPath.trim()}/${e.name}`, name: e.name, isDirectory: e.isDir })),
      };
      addProject(project);
      setActiveProject(project.root);
      onClose();
    } catch (err) {
      setError(`Cannot open: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setLoading(false);
    }
  };

  const handleOpen = () => openPath(path);

  const handleBrowse = async () => {
    try {
      const dialog = await import("@tauri-apps/plugin-dialog");
      const selected = await dialog.open({ directory: true, multiple: false });
      if (selected) {
        const selectedPath = selected as string;
        setPath(selectedPath);
        // Auto-open the project immediately after selecting via native dialog
        try {
          await openPath(selectedPath);
        } catch (openErr) {
          console.error("[ProjectOpen] openPath failed:", openErr);
          // Path is already filled — user can retry with the button
        }
      }
    } catch (err) {
      console.error("[ProjectOpen] Browse dialog failed:", err);
      setError(`Dialog error: ${err instanceof Error ? err.message : "Unknown"}. Enter path manually.`);
    }
  };

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 1000,
      background: "rgba(0, 0, 0, 0.6)",
      display: "flex", alignItems: "center", justifyContent: "center",
      backdropFilter: "blur(4px)",
    }} role="dialog" aria-modal="true" aria-label="Open Project">
      <div style={{
        background: "var(--bg-elevated)",
        borderRadius: "var(--radius-lg)",
        padding: 28,
        width: 480,
        maxWidth: "90vw",
        boxShadow: "var(--shadow-lg)",
        border: "1px solid var(--border-default)",
      }}>
        <h2 style={{
          margin: "0 0 20px",
          fontSize: 16,
          fontWeight: 600,
          color: "var(--text-primary)",
          letterSpacing: -0.3,
        }}>
          Open Project
        </h2>

        <div style={{ marginBottom: 16 }}>
          <label style={{
            display: "block",
            fontSize: 12,
            color: "var(--text-secondary)",
            marginBottom: 6,
            fontWeight: 500,
          }}>
            Project directory
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              type="text"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleOpen()}
              placeholder="/path/to/project"
              style={{
                flex: 1,
                padding: "10px 14px",
                borderRadius: "var(--radius-md)",
                border: "1px solid var(--border-default)",
                background: "var(--bg-tertiary)",
                color: "var(--text-primary)",
                fontSize: 13,
              }}
            />
            <button onClick={handleBrowse} style={{
              padding: "10px 14px",
              borderRadius: "var(--radius-md)",
              border: "1px solid var(--border-default)",
              background: "var(--bg-tertiary)",
              color: "var(--text-secondary)",
              cursor: "pointer",
              fontSize: 12,
              fontWeight: 500,
            }}>
              Browse
            </button>
          </div>
        </div>

        {error && (
          <div style={{
            color: "var(--error)",
            fontSize: 12,
            marginBottom: 16,
            padding: "8px 12px",
            background: "var(--error-muted)",
            borderRadius: "var(--radius-sm)",
          }}>
            {error}
          </div>
        )}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 24 }}>
          <button onClick={onClose} style={{
            padding: "10px 20px",
            borderRadius: "var(--radius-md)",
            border: "1px solid var(--border-default)",
            background: "var(--bg-tertiary)",
            color: "var(--text-secondary)",
            cursor: "pointer",
            fontSize: 13,
            fontWeight: 500,
            transition: "all 0.12s",
          }}
          onMouseEnter={(e) => { e.currentTarget.style.background = "var(--bg-elevated)"; e.currentTarget.style.color = "var(--text-primary)"; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "var(--bg-tertiary)"; e.currentTarget.style.color = "var(--text-secondary)"; }}
          >
            Cancel
          </button>
          <button onClick={handleOpen} disabled={loading} style={{
            padding: "10px 24px",
            borderRadius: "var(--radius-md)",
            border: "1px solid var(--accent)",
            background: "var(--accent)",
            color: "var(--accent-text)",
            cursor: loading ? "wait" : "pointer",
            fontSize: 13,
            fontWeight: 600,
            opacity: loading ? 0.7 : 1,
            transition: "all 0.12s",
          }}
          onMouseEnter={(e) => { if (!loading) e.currentTarget.style.background = "var(--accent-hover)"; }}
          onMouseLeave={(e) => { if (!loading) e.currentTarget.style.background = "var(--accent)"; }}
          >
            {loading ? "Opening..." : "Open Project"}
          </button>
        </div>
      </div>
    </div>
  );
}
