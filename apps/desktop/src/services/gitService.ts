/**
 * Git Service — provides git blame data for the editor.
 */

import { process as ipcProcess } from "../ipc/client";

export interface GitBlameEntry {
  line: number;
  author: string;
  date: string;
}

export async function gitLogFile(filePath: string): Promise<GitBlameEntry[]> {
  try {
    const result = await ipcProcess.run(
      `git blame --date=short -p -L 1,2000 "${filePath}"`,
      "/",
    );

    const lines = result.stdout?.trim()?.split("\n")?.filter(Boolean) ?? [];
    if (lines.length === 0) return [];

    const results: GitBlameEntry[] = [];
    let currentLine = 0;
    let currentAuthor = "";

    for (const line of lines) {
      const match = line.match(/^[0-9a-f]{40}\s+(\d+)\s+(\d+)/);
      if (match) {
        currentLine = parseInt(match[1], 10);
      } else if (line.startsWith("author ")) {
        currentAuthor = line.slice(7);
      } else if (line.startsWith("author-time ")) {
        const ts = parseInt(line.slice(12), 10);
        const date = new Date(ts * 1000).toISOString().slice(0, 10);
        results.push({
          line: currentLine,
          author: currentAuthor,
          date,
        });
      }
    }

    return results;
  } catch (err) {
    console.error("[gitService] git blame failed:", err);
    return [];
  }
}
