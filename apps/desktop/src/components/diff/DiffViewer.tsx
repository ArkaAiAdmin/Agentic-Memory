/**
 * DiffViewer — Production-quality diff display with unified and side-by-side modes.
 *
 * Features:
 * - Myers diff algorithm for accurate comparison
 * - Word-level highlighting within changed lines
 * - Unified and side-by-side view modes
 * - Collapsible context regions
 * - Line numbers, hunk headers, add/remove stats
 * - Stage hunk button (when onStageHunk provided)
 */

import React, { useMemo, useState } from "react";
import { myersDiff, computeHunks, type DiffHunk, type DiffLine } from "./myersDiff";
import { wordDiff, type WordSegment } from "./wordDiff";

export interface DiffViewerProps {
  oldContent?: string;
  newContent?: string;
  /** Raw unified-diff text (when provided, oldContent/newContent are ignored) */
  rawDiff?: string;
  /** Legacy aliases */
  oldText?: string;
  newText?: string;
  language?: string;
  viewMode?: "unified" | "side-by-side";
  contextLines?: number;
  fileName?: string;
  onStageHunk?: (hunkIndex: number) => void;
}

export function DiffViewer(props: DiffViewerProps) {
  const [mode, setMode] = useState(props.viewMode ?? "unified");

  const { rawDiff, contextLines = 3, fileName, onStageHunk } = props;
  const oldContent = props.oldContent ?? props.oldText ?? "";
  const newContent = props.newContent ?? props.newText ?? "";

  const { hunks, stats } = useMemo(() => {
    const oldLines = oldContent.split("\n");
    const newLines = newContent.split("\n");
    const edits = myersDiff(oldLines, newLines);
    const hunks = computeHunks(oldLines, newLines, edits, contextLines);

    let additions = 0;
    let deletions = 0;
    for (const hunk of hunks) {
      for (const line of hunk.edits) {
        if (line.type === "added") additions++;
        if (line.type === "removed") deletions++;
      }
    }

    return { hunks, stats: { additions, deletions } };
  }, [oldContent, newContent, contextLines]);

  if (rawDiff) {
    return (
      <div style={{ height: "100%", display: "flex", flexDirection: "column", fontSize: 12 }}>
        <div style={{
          padding: "6px 12px",
          borderBottom: "1px solid var(--border-default)",
          background: "var(--bg-secondary)",
          flexShrink: 0,
        }}>
          {fileName && (
            <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-primary)", fontWeight: 500 }}>
              {fileName}
            </span>
          )}
        </div>
        <div style={{ flex: 1, overflow: "auto" }}>
          <RawDiffRenderer rawDiff={rawDiff} fileName={fileName} />
        </div>
      </div>
    );
  }

  if (hunks.length === 0) {
    return (
      <div style={{ padding: 16, textAlign: "center", color: "var(--text-tertiary)", fontSize: 12 }}>
        No differences
      </div>
    );
  }

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", fontSize: 12 }}>
      {/* Header */}
      <div style={{
        padding: "6px 12px",
        borderBottom: "1px solid var(--border-default)",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        background: "var(--bg-secondary)",
        flexShrink: 0,
      }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {fileName && (
            <span style={{ fontFamily: "var(--font-mono)", color: "var(--text-primary)", fontWeight: 500 }}>
              {fileName}
            </span>
          )}
          <span style={{ color: "var(--success)", fontSize: 11 }}>+{stats.additions}</span>
          <span style={{ color: "var(--error)", fontSize: 11 }}>-{stats.deletions}</span>
        </div>
        <div style={{ display: "flex", gap: 2 }}>
          <ModeButton label="Unified" active={mode === "unified"} onClick={() => setMode("unified")} />
          <ModeButton label="Split" active={mode === "side-by-side"} onClick={() => setMode("side-by-side")} />
        </div>
      </div>

      {/* Content */}
      <div style={{ flex: 1, overflow: "auto", fontFamily: "var(--font-mono)", fontSize: 12, lineHeight: 1.6 }}>
        {mode === "unified" ? (
          <UnifiedView hunks={hunks} onStageHunk={onStageHunk} />
        ) : (
          <SideBySideView hunks={hunks} onStageHunk={onStageHunk} />
        )}
      </div>
    </div>
  );
}

// ── Unified View ─────────────────────────────────────────────────────────────

function UnifiedView({ hunks, onStageHunk }: { hunks: DiffHunk[]; onStageHunk?: (i: number) => void }) {
  return (
    <div>
      {hunks.map((hunk, hunkIdx) => (
        <div key={hunkIdx}>
          {/* Hunk header */}
          <div style={{
            padding: "4px 12px",
            background: "var(--bg-hover)",
            color: "var(--accent)",
            fontSize: 11,
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            borderTop: hunkIdx > 0 ? "1px solid var(--border-subtle)" : "none",
          }}>
            <span>@@ -{hunk.oldStart},{hunk.oldCount} +{hunk.newStart},{hunk.newCount} @@</span>
            {onStageHunk && (
              <button
                onClick={() => onStageHunk(hunkIdx)}
                style={{
                  padding: "2px 8px", borderRadius: 3, border: "1px solid var(--border-default)",
                  background: "transparent", color: "var(--text-secondary)", fontSize: 10,
                  cursor: "pointer", fontWeight: 500,
                }}
              >
                Stage
              </button>
            )}
          </div>

          {/* Lines */}
          {hunk.edits.map((line, lineIdx) => (
            <UnifiedLine key={`${line.oldLineNum ?? "o"}-${line.newLineNum ?? "n"}-${lineIdx}`} line={line} hunk={hunk} lineIdx={lineIdx} />
          ))}
        </div>
      ))}
    </div>
  );
}

function UnifiedLine({ line, hunk, lineIdx }: { line: DiffLine; hunk: DiffHunk; lineIdx: number }) {
  // Word-level diff: pair adjacent removed/added lines
  const wordSegments = useMemo(() => {
    if (line.type === "removed") {
      const nextLine = hunk.edits[lineIdx + 1];
      if (nextLine?.type === "added") {
        return wordDiff(line.content, nextLine.content).oldSegments;
      }
    }
    if (line.type === "added") {
      const prevLine = hunk.edits[lineIdx - 1];
      if (prevLine?.type === "removed") {
        return wordDiff(prevLine.content, line.content).newSegments;
      }
    }
    return null;
  }, [line, hunk.edits, lineIdx]);

  const bgColor = line.type === "added" ? "rgba(34, 197, 94, 0.08)"
    : line.type === "removed" ? "rgba(239, 68, 68, 0.08)"
    : "transparent";

  const borderColor = line.type === "added" ? "var(--success)"
    : line.type === "removed" ? "var(--error)"
    : "transparent";

  const textColor = line.type === "added" ? "var(--success)"
    : line.type === "removed" ? "var(--error)"
    : "var(--text-secondary)";

  return (
    <div style={{
      display: "flex",
      background: bgColor,
      borderLeft: `3px solid ${borderColor}`,
      minHeight: 20,
    }}>
      {/* Old line number */}
      <span style={lineNumStyle}>
        {line.oldLineNum ?? ""}
      </span>
      {/* New line number */}
      <span style={lineNumStyle}>
        {line.newLineNum ?? ""}
      </span>
      {/* Gutter symbol */}
      <span style={{ width: 16, textAlign: "center", color: textColor, fontWeight: 700, userSelect: "none", flexShrink: 0 }}>
        {line.type === "added" ? "+" : line.type === "removed" ? "-" : " "}
      </span>
      {/* Content */}
      <span style={{ flex: 1, whiteSpace: "pre", color: textColor, paddingRight: 12 }}>
        {wordSegments ? <WordHighlight segments={wordSegments} baseType={line.type} /> : line.content}
      </span>
    </div>
  );
}

// ── Side-by-Side View ────────────────────────────────────────────────────────

function SideBySideView({ hunks, onStageHunk }: { hunks: DiffHunk[]; onStageHunk?: (i: number) => void }) {
  // Build paired rows: each row has left (old) and right (new) content
  const rows = useMemo(() => buildSideBySideRows(hunks), [hunks]);

  return (
    <div>
      {rows.map((section, sIdx) => (
        <div key={sIdx}>
          {section.header && (
            <div style={{
              padding: "4px 12px",
              background: "var(--bg-hover)",
              color: "var(--accent)",
              fontSize: 11,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              borderTop: sIdx > 0 ? "1px solid var(--border-subtle)" : "none",
            }}>
              <span>{section.header}</span>
              {onStageHunk && (
                <button
                  onClick={() => onStageHunk(sIdx)}
                  style={{
                    padding: "2px 8px", borderRadius: 3, border: "1px solid var(--border-default)",
                    background: "transparent", color: "var(--text-secondary)", fontSize: 10,
                    cursor: "pointer", fontWeight: 500,
                  }}
                >
                  Stage
                </button>
              )}
            </div>
          )}
          {section.rows.map((row, rIdx) => (
            <div key={`${row.left?.lineNum ?? "o"}-${row.right?.lineNum ?? "n"}-${rIdx}`} style={{ display: "flex" }}>
              {/* Left (old) */}
              <div style={{
                flex: 1,
                display: "flex",
                background: row.left?.type === "removed" ? "rgba(239, 68, 68, 0.08)" : "transparent",
                borderRight: "1px solid var(--border-subtle)",
                minHeight: 20,
              }}>
                <span style={lineNumStyle}>{row.left?.lineNum ?? ""}</span>
                <span style={{
                  flex: 1, whiteSpace: "pre", paddingRight: 8,
                  color: row.left?.type === "removed" ? "var(--error)" : "var(--text-secondary)",
                }}>
                  {row.left?.content ?? ""}
                </span>
              </div>
              {/* Right (new) */}
              <div style={{
                flex: 1,
                display: "flex",
                background: row.right?.type === "added" ? "rgba(34, 197, 94, 0.08)" : "transparent",
                minHeight: 20,
              }}>
                <span style={lineNumStyle}>{row.right?.lineNum ?? ""}</span>
                <span style={{
                  flex: 1, whiteSpace: "pre", paddingRight: 8,
                  color: row.right?.type === "added" ? "var(--success)" : "var(--text-secondary)",
                }}>
                  {row.right?.content ?? ""}
                </span>
              </div>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

interface SideBySideRow {
  left: { content: string; lineNum: number; type: "context" | "removed" } | null;
  right: { content: string; lineNum: number; type: "context" | "added" } | null;
}

interface SideBySideSection {
  header: string | null;
  rows: SideBySideRow[];
}

function buildSideBySideRows(hunks: DiffHunk[]): SideBySideSection[] {
  return hunks.map((hunk) => {
    const rows: SideBySideRow[] = [];
    const removedQueue: DiffLine[] = [];
    const addedQueue: DiffLine[] = [];

    function flushQueues() {
      const max = Math.max(removedQueue.length, addedQueue.length);
      for (let i = 0; i < max; i++) {
        const left = removedQueue[i];
        const right = addedQueue[i];
        rows.push({
          left: left ? { content: left.content, lineNum: left.oldLineNum!, type: "removed" } : null,
          right: right ? { content: right.content, lineNum: right.newLineNum!, type: "added" } : null,
        });
      }
      removedQueue.length = 0;
      addedQueue.length = 0;
    }

    for (const line of hunk.edits) {
      if (line.type === "context") {
        flushQueues();
        rows.push({
          left: { content: line.content, lineNum: line.oldLineNum!, type: "context" },
          right: { content: line.content, lineNum: line.newLineNum!, type: "context" },
        });
      } else if (line.type === "removed") {
        removedQueue.push(line);
      } else {
        addedQueue.push(line);
      }
    }
    flushQueues();

    return {
      header: `@@ -${hunk.oldStart},${hunk.oldCount} +${hunk.newStart},${hunk.newCount} @@`,
      rows,
    };
  });
}

// ── Word Highlight Component ─────────────────────────────────────────────────

function WordHighlight({ segments, baseType }: { segments: WordSegment[]; baseType: "added" | "removed" | "context" }) {
  return (
    <>
      {segments.map((seg, i) => {
        if (seg.type === "equal") {
          return <span key={i}>{seg.text}</span>;
        }
        const highlight = baseType === "removed"
          ? "rgba(239, 68, 68, 0.3)"
          : "rgba(34, 197, 94, 0.3)";
        return (
          <span key={i} style={{ background: highlight, borderRadius: 2 }}>
            {seg.text}
          </span>
        );
      })}
    </>
  );
}

// ── Mode Toggle Button ───────────────────────────────────────────────────────

function ModeButton({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: "3px 8px",
        borderRadius: 3,
        border: "none",
        background: active ? "var(--bg-elevated)" : "transparent",
        color: active ? "var(--text-primary)" : "var(--text-tertiary)",
        fontSize: 10,
        fontWeight: 500,
        cursor: "pointer",
      }}
    >
      {label}
    </button>
  );
}

// ── Shared Styles ────────────────────────────────────────────────────────────

const lineNumStyle: React.CSSProperties = {
  width: 40,
  textAlign: "right",
  padding: "0 6px",
  color: "var(--text-tertiary)",
  userSelect: "none",
  fontSize: 10,
  flexShrink: 0,
  opacity: 0.7,
};

/** Renders raw unified-diff text (from git diff output) with proper coloring */
function RawDiffRenderer({ rawDiff, fileName }: { rawDiff: string; fileName?: string }) {
  const lines = rawDiff.split("\n");
  return (
    <div style={{ fontSize: 12, fontFamily: "var(--font-mono)" }}>
      {fileName && (
        <div style={{ padding: "4px 12px", fontSize: 11, color: "var(--text-tertiary)", borderBottom: "1px solid var(--border-subtle)" }}>
          {fileName}
        </div>
      )}
      {lines.map((line, i) => {
        const isAdded = line.startsWith("+") && !line.startsWith("+++");
        const isRemoved = line.startsWith("-") && !line.startsWith("---");
        const isHunk = line.startsWith("@@");
        const isMeta = line.startsWith("diff ") || line.startsWith("index ") || line.startsWith("---") || line.startsWith("+++");
        return (
          <div key={`${line.slice(0, 20)}-${i}`} style={{
            padding: "1px 12px", whiteSpace: "pre", lineHeight: 1.6, fontSize: 11,
            background: isAdded ? "rgba(34, 197, 94, 0.08)" : isRemoved ? "rgba(239, 68, 68, 0.08)" : isHunk ? "var(--bg-hover)" : "transparent",
            color: isAdded ? "var(--success)" : isRemoved ? "var(--error)" : isHunk ? "var(--accent)" : isMeta ? "var(--text-tertiary)" : "var(--text-secondary)",
            borderLeft: isAdded ? "3px solid var(--success)" : isRemoved ? "3px solid var(--error)" : "3px solid transparent",
          }}>
            {line}
          </div>
        );
      })}
    </div>
  );
}
