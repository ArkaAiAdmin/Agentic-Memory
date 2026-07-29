/**
 * Editor Context Service
 *
 * Reactive singleton that tracks live Monaco editor state:
 * - Active file path + content
 * - Cursor position (line, column)
 * - Selection range
 * - Open file tabs
 * - Language of active file
 *
 * Other services (completion, inline edit, @-mentions) subscribe here
 * instead of touching Monaco directly.
 */

export interface CursorPosition {
  line: number;
  column: number;
}

export interface SelectionRange {
  startLine: number;
  startColumn: number;
  endLine: number;
  endColumn: number;
}

export interface EditorContextState {
  activeFile: string | null;
  activeContent: string;
  language: string;
  cursor: CursorPosition;
  selection: SelectionRange | null;
  hasSelection: boolean;
  openTabs: Array<{ path: string; name: string }>;
}

export type EditorContextListener = (state: EditorContextState) => void;

const INITIAL_STATE: EditorContextState = {
  activeFile: null,
  activeContent: "",
  language: "",
  cursor: { line: 1, column: 1 },
  selection: null,
  hasSelection: false,
  openTabs: [],
};

class EditorContext {
  private state: EditorContextState = { ...INITIAL_STATE };
  private listeners = new Set<EditorContextListener>();

  /** Subscribe to editor context changes. Returns unsubscribe fn. */
  subscribe(listener: EditorContextListener): () => void {
    this.listeners.add(listener);
    // Emit current state immediately
    listener({ ...this.state });
    return () => this.listeners.delete(listener);
  }

  /** Get current state (snapshot). */
  getState(): EditorContextState {
    return { ...this.state };
  }

  /** Get the text of the current selection (empty string if no selection). */
  getSelectedText(): string {
    if (!this.state.selection || !this.state.hasSelection) return "";
    const lines = this.state.activeContent.split("\n");
    const sel = this.state.selection;

    // Single-line selection
    if (sel.startLine === sel.endLine) {
      const line = lines[sel.startLine - 1] ?? "";
      return line.slice(sel.startColumn - 1, sel.endColumn - 1);
    }

    // Multi-line selection
    const result: string[] = [];
    for (let i = sel.startLine - 1; i <= sel.endLine - 1 && i < lines.length; i++) {
      const line = lines[i];
      if (i === sel.startLine - 1) {
        result.push(line.slice(sel.startColumn - 1));
      } else if (i === sel.endLine - 1) {
        result.push(line.slice(0, sel.endColumn - 1));
      } else {
        result.push(line);
      }
    }
    return result.join("\n");
  }

  /** Get prefix/suffix around cursor for fill-in-the-middle completion. */
  getCompletionContext(maxChars = 2000): { prefix: string; suffix: string } | null {
    if (!this.state.activeFile) return null;

    const lines = this.state.activeContent.split("\n");
    const { line, column } = this.state.cursor;

    // Prefix: up to maxChars chars from before cursor
    const prefixLines = lines.slice(0, line);
    let prefix = prefixLines.join("\n").slice(-maxChars);

    // Suffix: up to maxChars chars from after cursor
    const suffixLines = lines.slice(line - 1);
    const currentLineSuffix = suffixLines[0]?.slice(column - 1) ?? "";
    const restSuffix = suffixLines.slice(1).join("\n").slice(0, maxChars);
    const suffix = currentLineSuffix + "\n" + restSuffix;

    return { prefix, suffix };
  }

  // ── Internal update methods (called by Monaco integration) ───────────

  /** Called when Monaco cursor or selection changes. */
  updateCursorAndSelection(
    cursor: CursorPosition,
    selection: SelectionRange | null,
  ) {
    this.state = {
      ...this.state,
      cursor,
      selection,
      hasSelection: selection !== null,
    };
    this.emit();
  }

  /** Called when the active file changes. */
  updateActiveFile(path: string | null, content: string, language: string) {
    this.state = {
      ...this.state,
      activeFile: path,
      activeContent: content,
      language,
    };
    this.emit();
  }

  /** Called when the active file's content changes (typing, external edit). */
  updateContent(content: string) {
    this.state = {
      ...this.state,
      activeContent: content,
    };
    // Don't emit on every keystroke — let the completion debounce handle timing
  }

  /** Called when open tabs change. */
  updateOpenTabs(tabs: Array<{ path: string; name: string }>) {
    this.state = {
      ...this.state,
      openTabs: tabs,
    };
    this.emit();
  }

  private emit() {
    const snapshot = { ...this.state };
    for (const listener of this.listeners) {
      try {
        listener(snapshot);
      } catch (err) {
        console.error("[EditorContext] Listener error:", err);
      }
    }
  }
}

/** Singleton editor context instance. */
export const editorContext = new EditorContext();
