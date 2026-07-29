/**
 * ComposerPanel — Multi-file diff review surface.
 *
 * Shows all pending change-sets with per-file diffs.
 * User can Accept All, Reject All, or toggle individual edits.
 */

import React, { useState, useEffect, useCallback } from "react";
import type { ChangeSet, FileEdit } from "@ami/shared";
import {
  getPendingChangeSets,
  applyChangeSet,
  revertChangeSet,
  discardChangeSet,
  onChangeSets,
} from "../../services/changeSet";
import { DiffViewer } from "../diff/DiffViewer";

interface Props {
  onOpenFile?: (path: string) => void;
}

const styles = {
  empty: {
    padding: 20, color: "var(--text-tertiary)", fontSize: 13, textAlign: "center" as const,
  },
  container: { display: "flex", flexDirection: "column" as const, height: "100%" },
  header: {
    padding: "8px 12px", borderBottom: "1px solid var(--border-default)",
    display: "flex", alignItems: "center", justifyContent: "space-between",
  },
  headerTitle: { fontSize: 12, color: "var(--text-secondary)" },
  applyAllBtn: {
    padding: "4px 10px", borderRadius: "var(--radius-sm)", border: "none",
    background: "var(--success)", color: "var(--text-inverse)", fontSize: 11, cursor: "pointer",
  },
  list: { flex: 1, overflow: "auto" },
  card: { borderBottom: "1px solid var(--border-default)" },
  cardSummary: (expanded: boolean) => ({
    padding: "8px 12px", display: "flex", alignItems: "center", gap: 8, cursor: "pointer",
    background: expanded ? "var(--bg-hover)" : "transparent",
  }),
  cardChevron: { fontSize: 10, color: "var(--text-tertiary)" },
  cardTitle: { fontSize: 12, color: "var(--text-primary)", flex: 1 },
  cardCount: { fontSize: 10, color: "var(--text-tertiary)" },
  cardBody: { padding: "4px 12px 8px" },
  cardActions: { display: "flex", gap: 6, marginTop: 8 },
  acceptBtn: (isApplying: boolean) => ({
    padding: "4px 10px", borderRadius: "var(--radius-sm)", border: "none",
    background: "var(--success)", color: "var(--text-inverse)", fontSize: 11, cursor: isApplying ? "wait" : "pointer",
  }),
  revertBtn: {
    padding: "4px 10px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
    background: "transparent", color: "var(--text-secondary)", fontSize: 11, cursor: "pointer",
  },
  discardBtn: {
    padding: "4px 10px", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)",
    background: "transparent", color: "var(--text-tertiary)", fontSize: 11, cursor: "pointer",
  },
};

export function ComposerPanel({ onOpenFile }: Props) {
  const [changeSets, setChangeSets] = useState<ChangeSet[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [applying, setApplying] = useState<string | null>(null);

  useEffect(() => {
    // Initialize from external change-set store — standard subscription pattern
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setChangeSets(getPendingChangeSets());
    return onChangeSets(setChangeSets);
  }, []);

  const toggleExpand = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleApply = useCallback(async (csId: string) => {
    setApplying(csId);
    const ok = await applyChangeSet(csId);
    setApplying(null);
    if (!ok) alert("Failed to apply change-set");
  }, []);

  const handleRevert = useCallback(async (csId: string) => {
    setApplying(csId);
    const ok = await revertChangeSet(csId);
    setApplying(null);
    if (!ok) alert("Failed to revert change-set");
  }, []);

  const handleDiscard = useCallback((csId: string) => {
    discardChangeSet(csId);
  }, []);

  const handleApplyAll = useCallback(async () => {
    for (const cs of changeSets) {
      await applyChangeSet(cs.id);
    }
  }, [changeSets]);

  if (changeSets.length === 0) {
    return <div style={styles.empty}>No pending changes. Agent edits will appear here for review.</div>;
  }

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <span style={styles.headerTitle}>
          {changeSets.length} pending change-set{changeSets.length !== 1 ? "s" : ""}
        </span>
        <button onClick={handleApplyAll} style={styles.applyAllBtn}>
          Apply All
        </button>
      </div>

      <div style={styles.list}>
        {changeSets.map((cs) => (
          <ChangeSetCard
            key={cs.id}
            changeSet={cs}
            isExpanded={expanded.has(cs.id)}
            isApplying={applying === cs.id}
            onToggle={() => toggleExpand(cs.id)}
            onApply={() => handleApply(cs.id)}
            onRevert={() => handleRevert(cs.id)}
            onDiscard={() => handleDiscard(cs.id)}
            onOpenFile={onOpenFile}
          />
        ))}
      </div>
    </div>
  );
}

function ChangeSetCard({
  changeSet,
  isExpanded,
  isApplying,
  onToggle,
  onApply,
  onRevert,
  onDiscard,
  onOpenFile,
}: {
  changeSet: ChangeSet;
  isExpanded: boolean;
  isApplying: boolean;
  onToggle: () => void;
  onApply: () => void;
  onRevert: () => void;
  onDiscard: () => void;
  onOpenFile?: (path: string) => void;
}) {
  const cs = changeSet;

  return (
    <div style={styles.card}>
      <div onClick={onToggle} style={styles.cardSummary(isExpanded)} role="button" tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            onToggle();
          }
        }}>
        <span style={styles.cardChevron}>{isExpanded ? "▼" : "▶"}</span>
        <span style={styles.cardTitle}>{cs.summary}</span>
        <span style={styles.cardCount}>
          {cs.edits.length} file{cs.edits.length !== 1 ? "s" : ""}
        </span>
      </div>

      {isExpanded && (
        <div style={styles.cardBody}>
          {cs.edits.map((edit, i) => (
            <FileEditRow key={i} edit={edit} onOpenFile={onOpenFile} />
          ))}

          <div style={styles.cardActions}>
            <button onClick={onApply} disabled={isApplying} style={styles.acceptBtn(isApplying)} aria-label="Accept changes">
              {isApplying ? "Applying..." : "Accept"}
            </button>
            <button onClick={onRevert} disabled={isApplying} style={styles.revertBtn} aria-label="Reject changes">
              Revert
            </button>
            <button onClick={onDiscard} style={styles.discardBtn}>
              Discard
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function FileEditRow({
  edit,
  onOpenFile,
}: {
  edit: FileEdit;
  onOpenFile?: (path: string) => void;
}) {
  const [showDiff, setShowDiff] = useState(false);
  const kindColor =
    edit.kind === "create" ? "var(--success)" : edit.kind === "delete" ? "var(--error)" : "var(--warning)";
  const kindLabel = edit.kind === "create" ? "A" : edit.kind === "delete" ? "D" : "M";

  return (
    <div>
      <div
        style={{
          padding: "3px 8px", display: "flex", alignItems: "center", gap: 8, fontSize: 12,
          cursor: onOpenFile ? "pointer" : "default",
        }}
        onClick={() => (edit.kind === "modify" && edit.oldText != null) ? setShowDiff(!showDiff) : onOpenFile?.(edit.path)}
      >
        <span style={{
          width: 16, height: 16, borderRadius: 3, background: kindColor,
          color: "var(--text-inverse)", display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: 10, fontWeight: 700,
        }}>
          {kindLabel}
        </span>
        <span style={{ color: "var(--text-primary)", flex: 1 }}>{edit.path}</span>
        {edit.kind === "modify" && edit.oldText != null && (
          <span style={{ fontSize: 10, color: "var(--text-tertiary)", cursor: "pointer" }}>
            {showDiff ? "Hide diff" : "Show diff"}
          </span>
        )}
      </div>
      {showDiff && edit.kind === "modify" && edit.oldText != null && edit.newText != null && (
        <div style={{ padding: "4px 8px 8px 32px" }}>
          <DiffViewer oldText={edit.oldText} newText={edit.newText} fileName={edit.path} />
        </div>
      )}
    </div>
  );
}
