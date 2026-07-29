import React, { useEffect, useState, useRef } from "react";
import { useAppStore } from "../stores/appStore";
import type { FileEntry } from "@ami/shared";

export function FileExplorer() {
  const { projects, activeProject, openFile, addProject } = useAppStore();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; entry: FileEntry } | null>(null);
  const contextMenuRef = useRef<HTMLDivElement>(null);

  const project = projects.find((p) => p.root === activeProject);

  useEffect(() => {
    function handleClick() {
      setContextMenu(null);
    }
    if (contextMenu) {
      document.addEventListener("click", handleClick);
      return () => document.removeEventListener("click", handleClick);
    }
  }, [contextMenu]);

  useEffect(() => {
    if (project && project.files.length === 0 && activeProject) {
      const loadFiles = async () => {
        try {
          const { fs } = await import("../ipc/client");
          const entries = await fs.listDir(activeProject);
          const fileEntries: FileEntry[] = entries.map((e) => ({
            path: `${activeProject}/${e.name}`,
            name: e.name,
            isDirectory: e.isDir,
          }));
          addProject({ root: activeProject, name: project.name, files: fileEntries });
        } catch (err) {
          console.error("Failed to load project files:", err);
        }
      };
      loadFiles();
    }
  }, [project, activeProject, addProject]);

  const handleContextMenu = (e: React.MouseEvent, entry: FileEntry) => {
    e.preventDefault();
    e.stopPropagation();
    setContextMenu({ x: e.clientX, y: e.clientY, entry });
  };

  const handleCopyPath = () => {
    if (contextMenu) {
      navigator.clipboard?.writeText(contextMenu.entry.path);
      setContextMenu(null);
    }
  };

  const handleReveal = () => {
    if (contextMenu) {
      // In a real implementation, reveal the file in the OS file manager
      console.log("Reveal:", contextMenu.entry.path);
      setContextMenu(null);
    }
  };

  const toggleExpand = (path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const handleFileClick = async (entry: FileEntry) => {
    if (entry.isDirectory) {
      toggleExpand(entry.path);
      return;
    }
    try {
      const { fs } = await import("../ipc/client");
      const content = await fs.readFile(entry.path);
      const ext = entry.name.split(".").pop() ?? "";
      openFile({ path: entry.path, name: entry.name, content, language: ext, isDirty: false, isAgentEdit: false });
    } catch (err) {
      console.error("Failed to open file:", err);
    }
  };

  const renderEntry = (entry: FileEntry, depth: number) => (
    <div key={entry.path}>
      <div
        onClick={() => handleFileClick(entry)}
        onContextMenu={(e) => handleContextMenu(e, entry)}
        role="treeitem"
        style={{
          padding: "3px 12px",
          paddingLeft: 12 + depth * 14,
          cursor: "pointer",
          fontSize: 12,
          display: "flex",
          alignItems: "center",
          gap: 5,
          color: entry.isDirectory ? "var(--text-primary)" : "var(--text-secondary)",
          borderRadius: "var(--radius-xs)",
          margin: "0 4px",
          transition: "background 0.1s",
        }}
        onMouseEnter={(e) => e.currentTarget.style.background = "var(--bg-hover)"}
        onMouseLeave={(e) => e.currentTarget.style.background = "transparent"}
      >
        {entry.isDirectory && (
          <span style={{ fontSize: 9, color: "var(--text-tertiary)", width: 12, textAlign: "center" }}>
            {expanded.has(entry.path) ? "▾" : "▸"}
          </span>
        )}
        {!entry.isDirectory && <span style={{ width: 12 }} />}
        <FileIcon name={entry.name} isDir={entry.isDirectory} />
        <span>{entry.name}</span>
      </div>
      {entry.isDirectory && expanded.has(entry.path) && entry.children?.map((child) => renderEntry(child, depth + 1))}
    </div>
  );

  return (
    <div style={{ padding: "8px 0" }} role="navigation" aria-label="File Explorer">
      <div style={{
        padding: "6px 12px",
        fontSize: 10,
        fontWeight: 600,
        color: "var(--text-tertiary)",
        textTransform: "uppercase",
        letterSpacing: 0.8,
      }}>
        Explorer
      </div>
      {project?.files.map((entry) => renderEntry(entry, 0))}
      {(!project || project.files.length === 0) && (
        <div style={{ padding: "12px", fontSize: 12, color: "var(--text-tertiary)", textAlign: "center" }}>
          No project open
        </div>
      )}
      {contextMenu && (
        <div
          ref={contextMenuRef}
          style={{
            position: "fixed",
            left: contextMenu.x,
            top: contextMenu.y,
            background: "var(--bg-elevated)",
            border: "1px solid var(--border-default)",
            borderRadius: "var(--radius-sm)",
            padding: "4px 0",
            minWidth: 160,
            boxShadow: "var(--shadow-md)",
            zIndex: 9999,
          }}
        >
          <ContextMenuItem label="Copy Path" onClick={handleCopyPath} />
          <ContextMenuItem label="Reveal in Finder" onClick={handleReveal} />
          {!contextMenu.entry.isDirectory && (
            <ContextMenuItem label="Open" onClick={() => { handleFileClick(contextMenu.entry); setContextMenu(null); }} />
          )}
        </div>
      )}
    </div>
  );
}

function ContextMenuItem({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <div
      onClick={onClick}
      style={{
        padding: "6px 16px",
        fontSize: 12,
        cursor: "pointer",
        color: "var(--text-primary)",
      }}
      onMouseEnter={(e) => e.currentTarget.style.background = "var(--bg-hover)"}
      onMouseLeave={(e) => e.currentTarget.style.background = "transparent"}
    >
      {label}
    </div>
  );
}

function FileIcon({ name, isDir }: { name: string; isDir: boolean }) {
  if (isDir) {
    return <span style={{ fontSize: 13 }}>📁</span>;
  }
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  const iconMap: Record<string, string> = {
    ts: "📄", tsx: "⚛", js: "📄", jsx: "⚛",
    py: "🐍", rs: "🦀", go: "📄", rb: "💎",
    json: "📋", yaml: "📋", yml: "📋", toml: "📋",
    md: "📝", html: "🌐", css: "🎨", scss: "🎨",
    svg: "🖼", png: "🖼", jpg: "🖼", jpeg: "🖼",
    sh: "💻", bash: "💻", zsh: "💻",
    lock: "🔒",
  };
  return <span style={{ fontSize: 13 }}>{iconMap[ext] ?? "📄"}</span>;
}
