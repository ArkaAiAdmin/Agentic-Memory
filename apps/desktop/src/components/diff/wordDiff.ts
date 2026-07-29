/**
 * Word-level diff — highlights specific changed tokens within modified lines.
 *
 * Used to show exactly which words changed in a replaced line pair,
 * providing finer granularity than line-level coloring.
 */

export interface WordSegment {
  text: string;
  type: "equal" | "added" | "removed";
}

/**
 * Compute word-level diff between two strings (a removed line vs an added line).
 * Returns segments for the old line and new line separately.
 */
export function wordDiff(
  oldText: string,
  newText: string,
): { oldSegments: WordSegment[]; newSegments: WordSegment[] } {
  const oldTokens = tokenize(oldText);
  const newTokens = tokenize(newText);

  const lcs = longestCommonSubsequence(oldTokens, newTokens);

  const oldSegments: WordSegment[] = [];
  const newSegments: WordSegment[] = [];

  let oi = 0;
  let ni = 0;
  let li = 0;

  while (li < lcs.length) {
    // Emit removed tokens from old before this LCS match
    while (oi < lcs[li].oldIdx) {
      oldSegments.push({ text: oldTokens[oi], type: "removed" });
      oi++;
    }
    // Emit added tokens from new before this LCS match
    while (ni < lcs[li].newIdx) {
      newSegments.push({ text: newTokens[ni], type: "added" });
      ni++;
    }
    // Emit common token
    oldSegments.push({ text: oldTokens[oi], type: "equal" });
    newSegments.push({ text: newTokens[ni], type: "equal" });
    oi++;
    ni++;
    li++;
  }

  // Remaining tokens after last LCS match
  while (oi < oldTokens.length) {
    oldSegments.push({ text: oldTokens[oi], type: "removed" });
    oi++;
  }
  while (ni < newTokens.length) {
    newSegments.push({ text: newTokens[ni], type: "added" });
    ni++;
  }

  return { oldSegments, newSegments };
}

/**
 * Tokenize a string into words and whitespace segments.
 * Preserves whitespace as separate tokens for accurate reconstruction.
 */
function tokenize(text: string): string[] {
  // Split into word and non-word segments, preserving everything
  const tokens: string[] = [];
  const regex = /(\s+|[^\s]+)/g;
  let match: RegExpExecArray | null;
  while ((match = regex.exec(text)) !== null) {
    tokens.push(match[1]);
  }
  return tokens;
}

interface LCSMatch {
  oldIdx: number;
  newIdx: number;
}

/**
 * Standard LCS for token arrays (O(nm) DP).
 * For word-level diffs within a single line, n and m are small.
 */
function longestCommonSubsequence(a: string[], b: string[]): LCSMatch[] {
  const n = a.length;
  const m = b.length;

  if (n === 0 || m === 0) return [];

  // DP table
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));

  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      if (a[i - 1] === b[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
      }
    }
  }

  // Backtrack
  const result: LCSMatch[] = [];
  let i = n;
  let j = m;
  while (i > 0 && j > 0) {
    if (a[i - 1] === b[j - 1]) {
      result.push({ oldIdx: i - 1, newIdx: j - 1 });
      i--;
      j--;
    } else if (dp[i - 1][j] > dp[i][j - 1]) {
      i--;
    } else {
      j--;
    }
  }

  result.reverse();
  return result;
}
