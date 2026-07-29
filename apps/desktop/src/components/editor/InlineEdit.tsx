/**
 * InlineEdit — Cmd+K floating edit widget.
 *
 * Appears over the current selection (or at cursor).
 * User types a prompt, the LLM generates a replacement,
 * rendered as an inline diff with Accept/Reject.
 */

import React, { useState, useRef, useEffect, useCallback } from "react";
import { useAppStore } from "../../stores/appStore";
import { createProvider } from "@ami/llm";
import { editorContext } from "../../services/editorContext";

interface Props {
  /** Whether the inline edit widget is visible. */
  isOpen: boolean;
  /** Called when the user closes the widget (Escape or after apply). */
  onClose: () => void;
  /** Called with the replacement text when the user accepts. */
  onApply: (newText: string) => void;
}

interface DiffLine {
  type: "added" | "removed" | "unchanged";
  content: string;
}

/** Compute a simple line-by-line diff between old and new text. */
function computeDiff(oldText: string, newText: string): DiffLine[] {
  const oldLines = oldText.split("\n");
  const newLines = newText.split("\n");
  const result: DiffLine[] = [];

  // Simple LCS-based diff
  const m = oldLines.length;
  const n = newLines.length;

  // For short texts, just do line-by-line comparison
  if (m + n < 200) {
    // Build LCS table
    const dp: number[][] = Array.from({ length: m + 1 }, () =>
      new Array(n + 1).fill(0),
    );
    for (let i = 1; i <= m; i++) {
      for (let j = 1; j <= n; j++) {
        if (oldLines[i - 1] === newLines[j - 1]) {
          dp[i][j] = dp[i - 1][j - 1] + 1;
        } else {
          dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
        }
      }
    }

    // Backtrack
    let i = m;
    let j = n;
    const temp: DiffLine[] = [];
    while (i > 0 || j > 0) {
      if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
        temp.unshift({ type: "unchanged", content: oldLines[i - 1] });
        i--;
        j--;
      } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
        temp.unshift({ type: "added", content: newLines[j - 1] });
        j--;
      } else {
        temp.unshift({ type: "removed", content: oldLines[i - 1] });
        i--;
      }
    }
    result.push(...temp);
  } else {
    // For large diffs, just show old as removed and new as added
    for (const line of oldLines) {
      result.push({ type: "removed", content: line });
    }
    for (const line of newLines) {
      result.push({ type: "added", content: line });
    }
  }

  return result;
}

export function InlineEdit({ isOpen, onClose, onApply }: Props) {
  const [prompt, setPrompt] = useState("");
  const [isGenerating, setIsGenerating] = useState(false);
  const [diff, setDiff] = useState<DiffLine[] | null>(null);
  const [generatedText, setGeneratedText] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (isOpen) {
      // Reset form state when dialog opens — syncing with prop change
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setPrompt("");
      setDiff(null);
      setGeneratedText(null);
      requestAnimationFrame(() => inputRef.current?.focus());
    } else {
      abortRef.current?.abort();
      abortRef.current = null;
    }
  }, [isOpen]);

  const generate = useCallback(async () => {
    const state = editorContext.getState();
    if (!state.hasSelection || !prompt.trim()) return;

    abortRef.current?.abort();
    abortRef.current = new AbortController();
    const signal = abortRef.current.signal;

    setIsGenerating(true);
    try {
      const selectedText = editorContext.getSelectedText();
      const { prefix, suffix } = editorContext.getCompletionContext(4000) ?? {
        prefix: state.activeContent.slice(0, Math.max(0, state.cursor.line * 80 - 80)),
        suffix: state.activeContent.slice(state.cursor.line * 80),
      };

      if (signal.aborted) return;

      const providerConfig = useAppStore.getState().providerConfig;

      const provider = createProvider(providerConfig);
      await provider.start();

      try {
        const systemPrompt = `You are a code editor. The user has selected some code and wants you to transform it according to their instruction.

Output ONLY the replacement code. No explanation, no markdown fences, no preamble.`;

        const userMessage = `## Current file prefix
\`\`\`
${prefix.slice(-2000)}
\`\`\`

## Selected code
\`\`\`
${selectedText}
\`\`\`

## File suffix
\`\`\`
${suffix.slice(0, 2000)}
\`\`\`

## Instruction
${prompt.trim()}

## Replacement code`;

        const response = await provider.chat({
          model: providerConfig.model ?? "gpt-4o",
          messages: [{ role: "user", content: userMessage }],
          tools: [],
          systemPrompt,
          maxTokens: 2048,
          temperature: 0.3,
        });

        let result = "";
        for await (const chunk of response) {
          if (signal.aborted) break;
          if (chunk.type === "text") {
            result += chunk.text;
          }
        }

        if (signal.aborted) return;

        let cleaned = result.trim();
        cleaned = cleaned.replace(/^```[\w]*\n?/, "").replace(/\n?```$/, "");

        setGeneratedText(cleaned);
        setDiff(computeDiff(selectedText, cleaned));
      } finally {
        await provider.stop();
      }
    } catch (err) {
      if (signal.aborted) return;
      console.error("Inline edit generation failed:", err);
    } finally {
      setIsGenerating(false);
    }
  }, [prompt]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        if (diff && generatedText) {
          onApply(generatedText);
          onClose();
        } else {
          generate();
        }
      }
    },
    [diff, generatedText, onClose, onApply, generate],
  );

  if (!isOpen) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Inline Edit"
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        zIndex: 9998,
        display: "flex",
        justifyContent: "center",
        paddingTop: "20vh",
        background: "rgba(0, 0, 0, 0.4)",
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        style={{
          width: 560,
          maxHeight: 500,
          background: "#1e1e2e",
          borderRadius: 12,
          border: "1px solid #2a2a4a",
          boxShadow: "0 16px 48px rgba(0,0,0,0.4)",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
        onKeyDown={handleKeyDown}
      >
        {/* Header */}
        <div
          style={{
            padding: "10px 16px",
            borderBottom: "1px solid #2a2a4a",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            fontSize: 12,
            color: "#888",
          }}
        >
          <span>Inline Edit</span>
          <span>Cmd+Enter to generate / apply</span>
        </div>

        {/* Prompt input */}
        <div style={{ padding: "8px 16px", borderBottom: "1px solid #2a2a4a" }}>
          <textarea
            ref={inputRef}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Describe the edit (e.g. 'add error handling', 'extract to function')"
            aria-label="Edit instruction"
            rows={2}
            style={{
              width: "100%",
              background: "#16213e",
              border: "1px solid #2a2a4a",
              borderRadius: 6,
              padding: "8px 10px",
              color: "#e0e0e0",
              fontSize: 13,
              fontFamily: "inherit",
              resize: "none",
              outline: "none",
            }}
          />
        </div>

        {/* Diff view */}
        <div style={{ flex: 1, overflow: "auto", padding: "8px 0" }}>
          {isGenerating && (
            <div
              style={{
                padding: "16px",
                textAlign: "center",
                color: "#888",
                fontSize: 13,
              }}
            >
              Generating...
            </div>
          )}

          {diff && (
            <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 12 }}>
              {diff.map((line, i) => (
                <div
                  key={line.content + i}
                  style={{
                    padding: "1px 16px",
                    background:
                      line.type === "added"
                        ? "rgba(34, 197, 94, 0.1)"
                        : line.type === "removed"
                          ? "rgba(239, 68, 68, 0.1)"
                          : "transparent",
                    borderLeft:
                      line.type === "added"
                        ? "3px solid #22c55e"
                        : line.type === "removed"
                          ? "3px solid #ef4444"
                          : "3px solid transparent",
                    whiteSpace: "pre",
                    color: line.type === "removed" ? "#ef4444" : "#e0e0e0",
                  }}
                >
                  {line.type === "added" ? "+ " : line.type === "removed" ? "- " : "  "}
                  {line.content}
                </div>
              ))}
            </div>
          )}

          {!isGenerating && !diff && prompt.trim() && (
            <div
              style={{
                padding: "16px",
                textAlign: "center",
                color: "#666",
                fontSize: 13,
              }}
            >
              Press Cmd+Enter to generate
            </div>
          )}
        </div>

        {/* Actions */}
        {diff && (
          <div
            style={{
              padding: "8px 16px",
              borderTop: "1px solid #2a2a4a",
              display: "flex",
              justifyContent: "flex-end",
              gap: 8,
            }}
          >
            <button
              onClick={onClose}
              aria-label="Reject changes"
              style={{
                padding: "6px 14px",
                borderRadius: 6,
                border: "1px solid #2a2a4a",
                background: "transparent",
                color: "#888",
                fontSize: 12,
                cursor: "pointer",
              }}
            >
              Reject
            </button>
            <button
              onClick={() => {
                if (generatedText) onApply(generatedText);
                onClose();
              }}
              aria-label="Accept changes"
              style={{
                padding: "6px 14px",
                borderRadius: 6,
                border: "none",
                background: "#58a6ff",
                color: "#fff",
                fontSize: 12,
                cursor: "pointer",
              }}
            >
              Accept
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
