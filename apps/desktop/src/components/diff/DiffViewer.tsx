import React from "react";

interface DiffLine {
  type: "added" | "removed" | "context";
  content: string;
  oldLineNum?: number;
  newLineNum?: number;
}

interface DiffViewerProps {
  oldContent: string;
  newContent: string;
  language?: string;
  viewMode?: "side-by-side" | "unified";
}

export function DiffViewer({
  oldContent,
  newContent,
  language,
  viewMode = "unified",
}: DiffViewerProps) {
  // Simple line-by-line diff
  const oldLines = oldContent.split("\n");
  const newLines = newContent.split("\n");
  const diffLines = computeDiff(oldLines, newLines);

  return (
    <div
      style={{
        fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
        fontSize: 12,
        lineHeight: 1.5,
        overflow: "auto",
        height: "100%",
      }}
    >
      {diffLines.map((line, i) => (
        <div
          key={i}
          style={{
            display: "flex",
            background:
              line.type === "added"
                ? "#1b4332"
                : line.type === "removed"
                  ? "#462222"
                  : "transparent",
            color:
              line.type === "added"
                ? "#95d5b2"
                : line.type === "removed"
                  ? "#f4a0a0"
                  : "#888",
          }}
        >
          <span
            style={{
              width: 40,
              textAlign: "right",
              padding: "0 8px",
              color: "#555",
              userSelect: "none",
              fontSize: 10,
            }}
          >
            {line.oldLineNum ?? ""}
          </span>
          <span
            style={{
              width: 40,
              textAlign: "right",
              padding: "0 8px",
              color: "#555",
              userSelect: "none",
              fontSize: 10,
            }}
          >
            {line.newLineNum ?? ""}
          </span>
          <span
            style={{
              width: 20,
              textAlign: "center",
              userSelect: "none",
              fontWeight: 700,
            }}
          >
            {line.type === "added" ? "+" : line.type === "removed" ? "-" : " "}
          </span>
          <span style={{ flex: 1, whiteSpace: "pre" }}>{line.content}</span>
        </div>
      ))}
    </div>
  );
}

/**
 * Simple LCS-based diff computation.
 */
function computeDiff(oldLines: string[], newLines: string[]): DiffLine[] {
  const result: DiffLine[] = [];

  // Simple approach: compare line by line
  let oldIdx = 0;
  let newIdx = 0;
  let oldLineNum = 1;
  let newLineNum = 1;

  while (oldIdx < oldLines.length || newIdx < newLines.length) {
    if (oldIdx >= oldLines.length) {
      result.push({
        type: "added",
        content: newLines[newIdx],
        newLineNum: newLineNum++,
      });
      newIdx++;
    } else if (newIdx >= newLines.length) {
      result.push({
        type: "removed",
        content: oldLines[oldIdx],
        oldLineNum: oldLineNum++,
      });
      oldIdx++;
    } else if (oldLines[oldIdx] === newLines[newIdx]) {
      result.push({
        type: "context",
        content: oldLines[oldIdx],
        oldLineNum: oldLineNum++,
        newLineNum: newLineNum++,
      });
      oldIdx++;
      newIdx++;
    } else {
      // Check if line was removed
      const nextOldMatch = newLines.indexOf(oldLines[oldIdx], newIdx);
      const nextNewMatch = oldLines.indexOf(newLines[newIdx], oldIdx);

      if (nextOldMatch === -1 && nextNewMatch === -1) {
        // Both changed
        result.push({
          type: "removed",
          content: oldLines[oldIdx],
          oldLineNum: oldLineNum++,
        });
        result.push({
          type: "added",
          content: newLines[newIdx],
          newLineNum: newLineNum++,
        });
        oldIdx++;
        newIdx++;
      } else if (nextNewMatch !== -1 && (nextOldMatch === -1 || nextNewMatch - oldIdx <= nextOldMatch - newIdx)) {
        // Lines were added
        while (newIdx < nextNewMatch) {
          result.push({
            type: "added",
            content: newLines[newIdx],
            newLineNum: newLineNum++,
          });
          newIdx++;
        }
      } else {
        // Lines were removed
        while (oldIdx < nextOldMatch) {
          result.push({
            type: "removed",
            content: oldLines[oldIdx],
            oldLineNum: oldLineNum++,
          });
          oldIdx++;
        }
      }
    }
  }

  return result;
}
