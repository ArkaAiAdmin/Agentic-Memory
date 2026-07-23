/**
 * Workspace Manager
 *
 * Manages project awareness: file watching, git status, project indexing.
 * Integrates with the Tauri Rust backend for filesystem operations.
 */

import type {
  Project,
  FileEntry,
  WorkspaceContext,
  FileChange,
} from "@ami/shared";

export class WorkspaceManager {
  private projects: Map<string, Project> = new Map();
  private activeFiles: Set<string> = new Set();
  private recentChanges: FileChange[] = [];

  /**
   * Open a project directory.
   */
  async openProject(root: string): Promise<Project> {
    const name = root.split("/").pop() ?? root;

    const project: Project = {
      root,
      name,
      files: [],
    };

    // Index project structure
    project.files = await this.indexDirectory(root, 3);

    this.projects.set(root, project);
    return project;
  }

  /**
   * Close a project.
   */
  closeProject(root: string): void {
    this.projects.delete(root);
  }

  /**
   * Get workspace context for the context builder.
   */
  getContext(): WorkspaceContext {
    const allFiles: FileEntry[] = [];
    for (const project of this.projects.values()) {
      allFiles.push(...project.files);
    }

    return {
      projectStructure: allFiles,
      activeFiles: [...this.activeFiles],
      gitStatus: "", // Populated by git tool
      recentChanges: this.recentChanges.slice(-20),
    };
  }

  /**
   * Track an active file (opened in editor).
   */
  setActiveFile(path: string): void {
    this.activeFiles.add(path);
  }

  /**
   * Remove an active file.
   */
  removeActiveFile(path: string): void {
    this.activeFiles.delete(path);
  }

  /**
   * Get active files.
   */
  getActiveFiles(): string[] {
    return [...this.activeFiles];
  }

  /**
   * Record a file change.
   */
  recordFileChange(path: string, changeType: FileChange["changeType"]): void {
    this.recentChanges.push({
      path,
      changeType,
      timestamp: Date.now(),
    });

    // Keep only last 100 changes
    if (this.recentChanges.length > 100) {
      this.recentChanges = this.recentChanges.slice(-100);
    }
  }

  /**
   * Get tags for the current workspace (for memory saves).
   */
  getTags(): string[] {
    const tags: string[] = [];
    for (const project of this.projects.values()) {
      tags.push(`project:${project.name}`);
    }
    return tags;
  }

  /**
   * Get all open projects.
   */
  getProjects(): Project[] {
    return [...this.projects.values()];
  }

  // ── Internal ──────────────────────────────────────────────────────────

  /**
   * Index a directory recursively (up to maxDepth).
   * Uses Tauri IPC for filesystem access.
   */
  private async indexDirectory(
    root: string,
    maxDepth: number,
    currentDepth = 0,
  ): Promise<FileEntry[]> {
    if (currentDepth >= maxDepth) return [];

    try {
      // In Tauri, this would use the fs plugin
      // For now, we use dynamic import of node:fs
      const fs = await import("node:fs/promises");
      const path = await import("node:path");

      const entries = await fs.readdir(root, { withFileTypes: true });
      const result: FileEntry[] = [];

      for (const entry of entries) {
        // Skip hidden files and common non-essential directories
        if (
          entry.name.startsWith(".") ||
          entry.name === "node_modules" ||
          entry.name === "target" ||
          entry.name === "dist" ||
          entry.name === "__pycache__"
        ) {
          continue;
        }

        const fullPath = path.join(root, entry.name);
        const fileEntry: FileEntry = {
          path: fullPath,
          name: entry.name,
          isDirectory: entry.isDirectory(),
        };

        if (entry.isDirectory()) {
          fileEntry.children = await this.indexDirectory(
            fullPath,
            maxDepth,
            currentDepth + 1,
          );
        }

        result.push(fileEntry);
      }

      return result;
    } catch {
      return [];
    }
  }
}
