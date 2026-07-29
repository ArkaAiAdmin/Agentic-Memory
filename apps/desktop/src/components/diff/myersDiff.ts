/**
 * Myers Diff Algorithm — O(ND) implementation.
 *
 * Produces the shortest edit script between two sequences of lines.
 * Based on Eugene W. Myers' 1986 paper "An O(ND) Difference Algorithm".
 */

export interface DiffEdit {
  type: "equal" | "insert" | "delete";
  oldStart: number; // 0-based index in old array
  oldEnd: number; // exclusive
  newStart: number; // 0-based index in new array
  newEnd: number; // exclusive
}

export interface DiffHunk {
  oldStart: number; // 1-based line number
  oldCount: number;
  newStart: number; // 1-based line number
  newCount: number;
  edits: DiffLine[];
}

export interface DiffLine {
  type: "context" | "added" | "removed";
  content: string;
  oldLineNum?: number; // 1-based
  newLineNum?: number; // 1-based
}

/**
 * Compute the shortest edit script using Myers' algorithm.
 * Returns a list of DiffEdit operations.
 */
export function myersDiff(oldLines: string[], newLines: string[]): DiffEdit[] {
  const N = oldLines.length;
  const M = newLines.length;
  const MAX = N + M;

  if (MAX === 0) return [];

  // Optimization: if one side is empty
  if (N === 0) {
    return [{ type: "insert", oldStart: 0, oldEnd: 0, newStart: 0, newEnd: M }];
  }
  if (M === 0) {
    return [{ type: "delete", oldStart: 0, oldEnd: N, newStart: 0, newEnd: 0 }];
  }

  // V[k] stores the furthest x position reached on diagonal k
  // We use offset to handle negative indices: V[k + offset]
  const offset = MAX;
  const size = 2 * MAX + 1;
  const V = new Int32Array(size);
  V.fill(-1);
  V[1 + offset] = 0;

  // Store trace for backtracking
  const trace: Int32Array[] = [];

  let found = false;
  for (let d = 0; d <= MAX; d++) {
    const snapshot = new Int32Array(V);
    trace.push(snapshot);

    for (let k = -d; k <= d; k += 2) {
      let x: number;

      // Decide whether to go down or right
      if (k === -d || (k !== d && V[k - 1 + offset] < V[k + 1 + offset])) {
        x = V[k + 1 + offset]; // move down (insert)
      } else {
        x = V[k - 1 + offset] + 1; // move right (delete)
      }

      let y = x - k;

      // Follow diagonal (equal lines)
      while (x < N && y < M && oldLines[x] === newLines[y]) {
        x++;
        y++;
      }

      V[k + offset] = x;

      if (x >= N && y >= M) {
        found = true;
        break;
      }
    }

    if (found) break;
  }

  // Backtrack to construct edit script
  return backtrack(trace, oldLines, newLines, offset);
}

/**
 * Backtrack through the trace to produce edits.
 */
function backtrack(
  trace: Int32Array[],
  oldLines: string[],
  newLines: string[],
  offset: number,
): DiffEdit[] {
  const N = oldLines.length;
  const M = newLines.length;

  let x = N;
  let y = M;
  const edits: DiffEdit[] = [];

  for (let d = trace.length - 1; d >= 0; d--) {
    const V = trace[d];
    const k = x - y;

    let prevK: number;
    if (k === -d || (k !== d && V[k - 1 + offset] < V[k + 1 + offset])) {
      prevK = k + 1; // came from above (insert)
    } else {
      prevK = k - 1; // came from left (delete)
    }

    const prevX = V[prevK + offset];
    const prevY = prevX - prevK;

    // Diagonal moves (equal)
    while (x > prevX && y > prevY) {
      x--;
      y--;
      edits.push({ type: "equal", oldStart: x, oldEnd: x + 1, newStart: y, newEnd: y + 1 });
    }

    if (d > 0) {
      if (x === prevX) {
        // Insert
        y--;
        edits.push({ type: "insert", oldStart: x, oldEnd: x, newStart: y, newEnd: y + 1 });
      } else {
        // Delete
        x--;
        edits.push({ type: "delete", oldStart: x, oldEnd: x + 1, newStart: y, newEnd: y });
      }
    }
  }

  edits.reverse();
  return mergeEdits(edits);
}

/**
 * Merge consecutive edits of the same type.
 */
function mergeEdits(edits: DiffEdit[]): DiffEdit[] {
  if (edits.length === 0) return [];

  const merged: DiffEdit[] = [edits[0]];
  for (let i = 1; i < edits.length; i++) {
    const prev = merged[merged.length - 1];
    const curr = edits[i];
    if (prev.type === curr.type && prev.oldEnd === curr.oldStart && prev.newEnd === curr.newStart) {
      prev.oldEnd = curr.oldEnd;
      prev.newEnd = curr.newEnd;
    } else {
      merged.push(curr);
    }
  }
  return merged;
}

/**
 * Convert raw DiffEdits into hunks with context lines.
 * contextLines: number of context lines to show around changes (default 3).
 */
export function computeHunks(
  oldLines: string[],
  newLines: string[],
  edits: DiffEdit[],
  contextLines = 3,
): DiffHunk[] {
  // Build all DiffLines with line numbers
  const allLines: DiffLine[] = [];
  for (const edit of edits) {
    switch (edit.type) {
      case "equal":
        for (let i = edit.oldStart; i < edit.oldEnd; i++) {
          const j = edit.newStart + (i - edit.oldStart);
          allLines.push({ type: "context", content: oldLines[i], oldLineNum: i + 1, newLineNum: j + 1 });
        }
        break;
      case "delete":
        for (let i = edit.oldStart; i < edit.oldEnd; i++) {
          allLines.push({ type: "removed", content: oldLines[i], oldLineNum: i + 1 });
        }
        break;
      case "insert":
        for (let j = edit.newStart; j < edit.newEnd; j++) {
          allLines.push({ type: "added", content: newLines[j], newLineNum: j + 1 });
        }
        break;
    }
  }

  // Find change regions and build hunks with context
  const hunks: DiffHunk[] = [];
  const changeIndices: number[] = [];
  for (let i = 0; i < allLines.length; i++) {
    if (allLines[i].type !== "context") {
      changeIndices.push(i);
    }
  }

  if (changeIndices.length === 0) return [];

  // Group changes that are within (contextLines * 2) of each other
  const groups: Array<[number, number]> = [];
  let groupStart = changeIndices[0];
  let groupEnd = changeIndices[0];

  for (let i = 1; i < changeIndices.length; i++) {
    if (changeIndices[i] - groupEnd <= contextLines * 2 + 1) {
      groupEnd = changeIndices[i];
    } else {
      groups.push([groupStart, groupEnd]);
      groupStart = changeIndices[i];
      groupEnd = changeIndices[i];
    }
  }
  groups.push([groupStart, groupEnd]);

  // Build hunks from groups
  for (const [gStart, gEnd] of groups) {
    const hunkStart = Math.max(0, gStart - contextLines);
    const hunkEnd = Math.min(allLines.length - 1, gEnd + contextLines);

    const hunkLines = allLines.slice(hunkStart, hunkEnd + 1);

    // Compute hunk header numbers
    const firstOld = hunkLines.find((l) => l.oldLineNum != null)?.oldLineNum ?? 1;
    const firstNew = hunkLines.find((l) => l.newLineNum != null)?.newLineNum ?? 1;
    const oldCount = hunkLines.filter((l) => l.type !== "added").length;
    const newCount = hunkLines.filter((l) => l.type !== "removed").length;

    hunks.push({
      oldStart: firstOld,
      oldCount,
      newStart: firstNew,
      newCount,
      edits: hunkLines,
    });
  }

  return hunks;
}

/**
 * Produce a flat list of all diff lines (no hunk grouping).
 * Useful for side-by-side display.
 */
export function computeAllDiffLines(
  oldLines: string[],
  newLines: string[],
  edits: DiffEdit[],
): DiffLine[] {
  const allLines: DiffLine[] = [];
  for (const edit of edits) {
    switch (edit.type) {
      case "equal":
        for (let i = edit.oldStart; i < edit.oldEnd; i++) {
          const j = edit.newStart + (i - edit.oldStart);
          allLines.push({ type: "context", content: oldLines[i], oldLineNum: i + 1, newLineNum: j + 1 });
        }
        break;
      case "delete":
        for (let i = edit.oldStart; i < edit.oldEnd; i++) {
          allLines.push({ type: "removed", content: oldLines[i], oldLineNum: i + 1 });
        }
        break;
      case "insert":
        for (let j = edit.newStart; j < edit.newEnd; j++) {
          allLines.push({ type: "added", content: newLines[j], newLineNum: j + 1 });
        }
        break;
    }
  }
  return allLines;
}
