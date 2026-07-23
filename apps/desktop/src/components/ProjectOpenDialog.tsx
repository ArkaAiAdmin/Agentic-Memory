import React, { useState } from "react";
import { useAppStore } from "../stores/appStore";
import type { Project } from "@ami/shared";

/**
 * Project Open Dialog
 *
 * Allows users to open a project directory.
 * In a full Tauri implementation, this uses the native file picker dialog.
 * Falls back to a text input for the path.
 */

export function ProjectOpenDialog({
  onClose,
}: {
  onClose: () => void;
}) {
  const { addProject, setActiveProject, theme } = useAppStore();
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const isDark = theme === "dark";

  const handleOpen = async () => {
    if (!path.trim()) {
      setError("Please enter a project path");
      return;
    }

    setLoading(true);
    setError(null);

    try {
      // In Tauri, we'd use the native dialog:
      // const { open } = await import("@tauri-apps/plugin-dialog");
      // const selected = await open({ directory: true, multiple: false });

      // For now, validate the path via IPC
      const { fs } = await import("../ipc/client");
      const entries = await fs.listDir(path.trim());

      // Extract project name from path
      const parts = path.trim().replace(/\/$/, "").split("/");
      const name = parts[parts.length - 1] || "Untitled";

      const project: Project = {
        root: path.trim(),
        name,
        files: entries.map((e) => ({
          path: `${path.trim()}/${e.name}`,
          name: e.name,
          isDirectory: e.isDir,
        })),
      };

      addProject(project);
      setActiveProject(project.root);
      onClose();
    } catch (err) {
      setError(
        `Cannot open directory: ${err instanceof Error ? err.message : "Unknown error"}`,
      );
    } finally {
      setLoading(false);
    }
  };

  const handleBrowse = async () => {
    try {
      const dialog = await import("@tauri-apps/plugin-dialog");
      const selected = await dialog.open({ directory: true, multiple: false });
      if (selected) {
        setPath(selected as string);
      }
    } catch {
      // Tauri dialog not available — user types path manually
      setError("Native dialog not available. Enter path manually.");
    }
  };

  return (
    <div
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: "rgba(0, 0, 0, 0.6)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
      }}
    >
      <div
        style={{
          background: isDark ? "#161b22" : "#fff",
          borderRadius: 12,
          padding: 24,
          width: 480,
          maxWidth: "90vw",
          boxShadow: "0 16px 48px rgba(0, 0, 0, 0.4)",
        }}
      >
        <h2
          style={{
            margin: "0 0 16px",
            fontSize: 16,
            fontWeight: 600,
            color: isDark ? "#c9d1d9" : "#24292f",
          }}
        >
          Open Project
        </h2>

        <div style={{ marginBottom: 12 }}>
          <label
            style={{
              display: "block",
              fontSize: 12,
              color: isDark ? "#8b949e" : "#57606a",
              marginBottom: 4,
            }}
          >
            Project directory path
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
                padding: "8px 12px",
                borderRadius: 6,
                border: `1px solid ${isDark ? "#30363d" : "#d0d7de"}`,
                background: isDark ? "#0d1117" : "#f6f8fa",
                color: isDark ? "#c9d1d9" : "#24292f",
                fontSize: 13,
                outline: "none",
              }}
            />
            <button
              onClick={handleBrowse}
              style={{
                padding: "8px 12px",
                borderRadius: 6,
                border: `1px solid ${isDark ? "#30363d" : "#d0d7de"}`,
                background: isDark ? "#21262d" : "#e1e4e8",
                color: isDark ? "#c9d1d9" : "#24292f",
                cursor: "pointer",
                fontSize: 12,
                whiteSpace: "nowrap",
              }}
            >
              Browse...
            </button>
          </div>
        </div>

        {error && (
          <div
            style={{
              color: "#f85149",
              fontSize: 12,
              marginBottom: 12,
              padding: "6px 10px",
              background: "rgba(248, 81, 73, 0.1)",
              borderRadius: 6,
            }}
          >
            {error}
          </div>
        )}

        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: 8,
            marginTop: 16,
          }}
        >
          <button
            onClick={onClose}
            style={{
              padding: "8px 16px",
              borderRadius: 6,
              border: `1px solid ${isDark ? "#30363d" : "#d0d7de"}`,
              background: "transparent",
              color: isDark ? "#c9d1d9" : "#24292f",
              cursor: "pointer",
              fontSize: 13,
            }}
          >
            Cancel
          </button>
          <button
            onClick={handleOpen}
            disabled={loading}
            style={{
              padding: "8px 16px",
              borderRadius: 6,
              border: "none",
              background: "#238636",
              color: "#fff",
              cursor: loading ? "wait" : "pointer",
              fontSize: 13,
              fontWeight: 500,
              opacity: loading ? 0.7 : 1,
            }}
          >
            {loading ? "Opening..." : "Open"}
          </button>
        </div>
      </div>
    </div>
  );
}
