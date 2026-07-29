/**
 * Change-Set Service
 *
 * Manages proposed file edits from the agent.
 * - Captures pre-images before writes
 * - Stages FileEdits into ChangeSets
 * - Applies/reverts atomically via Rust backend
 * - Persists checkpoints for undo
 */

import type { ChangeSet, FileEdit } from "@ami/shared";
import { fs as fsIpc } from "../ipc/client";
import { nanoid } from "nanoid";
import { memoryBridge } from "@ami/memory-bridge";

// ── State ────────────────────────────────────────────────────────────────

const pendingChangeSets = new Map<string, ChangeSet>();
const appliedChangeSets = new Map<string, ChangeSet>();
const listeners = new Set<(changeSets: ChangeSet[]) => void>();
const applying = new Set<string>();

function notifyListener() {
  const cs = getPendingChangeSets();
  for (const fn of listeners) fn(cs);
}

// ── Public API ───────────────────────────────────────────────────────────

/** Get all pending (not yet applied) change sets. */
export function getPendingChangeSets(): ChangeSet[] {
  return Array.from(pendingChangeSets.values()).sort(
    (a, b) => b.createdAt - a.createdAt,
  );
}

/** Get applied change sets (for history/undo). */
export function getAppliedChangeSets(): ChangeSet[] {
  return Array.from(appliedChangeSets.values()).sort(
    (a, b) => b.createdAt - a.createdAt,
  );
}

/** Subscribe to change-set changes. Returns unsubscribe fn. */
export function onChangeSets(fn: (changeSets: ChangeSet[]) => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

/**
 * Create a ChangeSet from a list of FileEdits.
 * Pre-images are captured automatically for modify/delete edits.
 */
export async function createChangeSet(
  summary: string,
  edits: FileEdit[],
): Promise<ChangeSet> {
  // Capture pre-images for modify/delete edits
  const enrichedEdits: FileEdit[] = [];
  for (const edit of edits) {
    if (edit.kind === "modify" || edit.kind === "delete") {
      try {
        const existing = await fsIpc.readFile(edit.path);
        enrichedEdits.push({ ...edit, oldText: existing });
      } catch {
        // File doesn't exist yet — treat as create
        enrichedEdits.push({ ...edit, kind: "create" });
      }
    } else {
      enrichedEdits.push(edit);
    }
  }

  const cs: ChangeSet = {
    id: `cs-${nanoid(8)}`,
    summary,
    edits: enrichedEdits,
    createdAt: Date.now(),
    applied: false,
    reverted: false,
  };

  pendingChangeSets.set(cs.id, cs);
  notifyListener();
  return cs;
}

/**
 * Add a single FileEdit to an existing ChangeSet or create a new one.
 */
export async function addEditToChangeSet(
  changeSetId: string | null,
  summary: string,
  edit: FileEdit,
): Promise<ChangeSet> {
  if (changeSetId && pendingChangeSets.has(changeSetId)) {
    const cs = pendingChangeSets.get(changeSetId)!;
    // Capture pre-image if needed
    let enriched = edit;
    if ((edit.kind === "modify" || edit.kind === "delete") && !edit.oldText) {
      try {
        const existing = await fsIpc.readFile(edit.path);
        enriched = { ...edit, oldText: existing };
      } catch {
        enriched = { ...edit, kind: "create" };
      }
    }
    cs.edits.push(enriched);
    notifyListener();
    return cs;
  }
  return createChangeSet(summary, [edit]);
}

/**
 * Apply a ChangeSet — write all files atomically.
 * All-or-nothing: if any write fails, none are persisted.
 */
export async function applyChangeSet(changeSetId: string): Promise<boolean> {
  if (applying.has(changeSetId)) return false;
  applying.add(changeSetId);

  try {
    const cs = pendingChangeSets.get(changeSetId);
    if (!cs) return false;

    // Validate all paths first
    for (const edit of cs.edits) {
      if (!edit.path) return false;
    }

    // Capture pre-images for rollback
    const preImages = new Map<string, string | null>();
    for (const edit of cs.edits) {
      if (edit.kind === "modify" || edit.kind === "delete") {
        try {
          preImages.set(edit.path, await fsIpc.readFile(edit.path));
        } catch {
          preImages.set(edit.path, null);
        }
      } else {
        preImages.set(edit.path, null); // created files have no pre-image
      }
    }

    const applied: string[] = [];
    try {
      // Apply all edits
      for (const edit of cs.edits) {
        switch (edit.kind) {
          case "create":
          case "modify":
            if (edit.newText != null) {
              await fsIpc.writeFile(edit.path, edit.newText);
              applied.push(edit.path);
            }
            break;
          case "delete":
            await fsIpc.deleteFile(edit.path);
            applied.push(edit.path);
            break;
        }
      }

      // Mark as applied
      cs.applied = true;
      pendingChangeSets.delete(cs.id);
      appliedChangeSets.set(cs.id, cs);

      // Save to memory — this is the key differentiator
      await memoryBridge.save({
        content: `Applied change-set: ${cs.summary}\nFiles: ${cs.edits.map((e) => `${e.kind} ${e.path}`).join(", ")}`,
        category: "projects",
        tags: ["change-set", "applied"],
      });

      notifyListener();
      return true;
    } catch (err) {
      // Rollback: restore pre-images for files we already wrote
      for (const path of applied) {
        const pre = preImages.get(path);
        try {
          if (pre != null) {
            await fsIpc.writeFile(path, pre);
          } else {
            // Was a create — try to delete
            await fsIpc.deleteFile(path);
          }
        } catch (err) { console.error("[ChangeSet] Rollback failed:", err); }
      }
      console.error("Change-set apply failed (rolled back):", err);
      return false;
    }
  } finally {
    applying.delete(changeSetId);
  }
}

/**
 * Revert a ChangeSet — restore all pre-images.
 */
export async function revertChangeSet(changeSetId: string): Promise<boolean> {
  const cs = pendingChangeSets.get(changeSetId) ?? appliedChangeSets.get(changeSetId);
  if (!cs) return false;

  try {
    for (const edit of cs.edits) {
      if (edit.kind === "delete" && edit.oldText != null) {
        // Restore deleted file
        await fsIpc.writeFile(edit.path, edit.oldText);
      } else if ((edit.kind === "modify" || edit.kind === "create") && edit.oldText != null) {
        // Restore original content
        await fsIpc.writeFile(edit.path, edit.oldText);
      } else if (edit.kind === "create" && !edit.oldText) {
        // New file with no pre-image — delete it
        await fsIpc.deleteFile(edit.path);
      }
    }

    cs.reverted = true;
    pendingChangeSets.delete(cs.id);
    appliedChangeSets.delete(cs.id);

    await memoryBridge.save({
      content: `Reverted change-set: ${cs.summary}`,
      category: "projects",
      tags: ["change-set", "reverted"],
    });

    notifyListener();
    return true;
  } catch (err) {
    console.error("Change-set revert failed:", err);
    return false;
  }
}

/**
 * Discard a pending ChangeSet (no file writes).
 */
export function discardChangeSet(changeSetId: string): void {
  pendingChangeSets.delete(changeSetId);
  notifyListener();
}
