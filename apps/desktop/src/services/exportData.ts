/**
 * Chat / Memory Export
 *
 * Utilities for exporting chat sessions as Markdown and memory notes as JSON.
 */

import type { ChatMessage } from "../stores/appStore";
import type { SearchResult } from "@ami/shared";

export function exportChatAsMarkdown(messages: ChatMessage[]): string {
  const lines: string[] = [
    "# Chat Session Export",
    `_Exported: ${new Date().toISOString()}_`,
    "",
  ];

  for (const msg of messages) {
    const role = msg.role === "user" ? "You" : msg.role === "assistant" ? "Agent" : msg.role;
    const time = new Date(msg.timestamp).toLocaleString();
    lines.push(`## ${role} — ${time}`);
    lines.push("");
    lines.push(msg.content ?? "");
    if (msg.toolCalls?.length) {
      lines.push("");
      lines.push("**Tool calls:**");
      for (const tc of msg.toolCalls) {
        lines.push(`- \`${tc.name}\`: ${tc.status}`);
      }
    }
    lines.push("");
    lines.push("---");
    lines.push("");
  }

  return lines.join("\n");
}

export function exportMemoriesAsJSON(memories: SearchResult[]): string {
  try {
    return JSON.stringify(memories, null, 2);
  } catch (err) {
    console.error("[exportData] JSON.stringify failed:", err);
    return "[]";
  }
}

export function exportMemoriesAsCSV(memories: SearchResult[]): string {
  const lines = ["note_id,category,score,content,tags"];
  for (const m of memories) {
    const content = `"${m.content.replace(/"/g, '""')}"`;
    const tags = `"${(m.tags?.join("; ") ?? "")}"`;
    lines.push(`${m.note_id},${m.category},${m.score.toFixed(4)},${content},${tags}`);
  }
  return lines.join("\n");
}

export function downloadBlob(content: string, filename: string, type = "text/plain") {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
