import React, { useEffect, useState } from "react";
import { useAppStore } from "../stores/appStore";
import type { FileEntry } from "@ami/shared";

export function FileExplorer() {
  const { projects, activeProject, openFile, addProject } = useAppStore();
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const project = projects.find((p) => p.root === activeProject);

  // Load files from filesystem if project has no files
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
          addProject({
            root: activeProject,
            name: project.name,
            files: fileEntries,
          });
        } catch (err) {
          console.error("Failed to load project files:", err);
        }
      };
      loadFiles();
    }
  }, [project, activeProject, addProject]);

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

    // Open file in editor
    try {
      const { fs } = await import("../ipc/client");
      const content = await fs.readFile(entry.path);
      const ext = entry.name.split(".").pop() ?? "";
      openFile({
        path: entry.path,
        name: entry.name,
        content,
        language: ext,
        isDirty: false,
        isAgentEdit: false,
      });
    } catch (err) {
      console.error("Failed to open file:", err);
    }
  };

  const renderEntry = (entry: FileEntry, depth: number) => (
    <div key={entry.path}>
      <div
        onClick={() => handleFileClick(entry)}
        style={{
          padding: "2px 8px",
          paddingLeft: 8 + depth * 16,
          cursor: "pointer",
          fontSize: 12,
          display: "flex",
          alignItems: "center",
          gap: 4,
          color: entry.isDirectory ? "#82aaff" : "#c0c0c0",
        }}
      >
        {entry.isDirectory && (
          <span style={{ fontSize: 10 }}>
            {expanded.has(entry.path) ? "▼" : "▶"}
          </span>
        )}
        <span>{entry.name}</span>
      </div>
      {entry.isDirectory &&
        expanded.has(entry.path) &&
        entry.children?.map((child) => renderEntry(child, depth + 1))}
    </div>
  );

  return (
    <div style={{ padding: "8px 0" }}>
      <div
        style={{
          padding: "4px 12px",
          fontSize: 11,
          fontWeight: 600,
          color: "#666",
          textTransform: "uppercase",
          letterSpacing: 1,
        }}
      >
        Explorer
      </div>
      {project?.files.map((entry) => renderEntry(entry, 0))}
    </div>
  );
}
