import React, { useRef, useEffect, useState } from "react";
import { useAppStore } from "../../stores/appStore";
import { editorContext } from "../../services/editorContext";
import { getCompletion, cancelCompletion } from "../../services/completion";

export function EditorPanel() {
  const { openFiles, activeFile } = useAppStore();
  const themeId = useAppStore((s) => s.theme);
  const activeFileData = openFiles.find((f) => f.path === activeFile);

  // Determine if current theme is light or dark for Monaco
  const isDark = !["daylight"].includes(themeId);

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", background: "var(--bg-primary)" }}>
      {/* Tab bar — only show when files are open */}
      {openFiles.length > 0 && (
      <div style={{
        display: "flex",
        overflow: "auto",
        background: "var(--bg-secondary)",
        borderBottom: "1px solid var(--border-default)",
        minHeight: 34,
        flexShrink: 0,
      }} role="tablist" aria-label="Open editors">
        {openFiles.map((file) => (
          <Tab
            key={file.path}
            file={file}
            isActive={file.path === activeFile}
          />
        ))}
      </div>
      )}

      {/* Breadcrumb */}
      {activeFileData && (
        <div style={{
          display: "flex", alignItems: "center", padding: "4px 12px",
          background: "var(--bg-secondary)", borderBottom: "1px solid var(--border-subtle)",
          fontSize: 11, color: "var(--text-tertiary)", gap: 2,
          minHeight: 24, flexShrink: 0,
        }}>
          {activeFileData.path.split("/").map((segment, i, arr) => (
            <span key={i} style={{ display: "flex", alignItems: "center", gap: 2 }}>
              {i > 0 && <span style={{ color: "var(--text-tertiary)", margin: "0 2px" }}>/</span>}
              <span style={{
                color: i === arr.length - 1 ? "var(--text-secondary)" : "var(--text-tertiary)",
                fontWeight: i === arr.length - 1 ? 500 : 400,
              }}>
                {segment}
              </span>
            </span>
          ))}
        </div>
      )}

      {/* Editor area */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {activeFileData ? (
          <MonacoEditor
            content={activeFileData.content}
            language={activeFileData.language}
            theme={isDark ? "vs-dark" : "vs"}
            filePath={activeFileData.path}
            _isDirty={activeFileData.isDirty}
          />
        ) : (
          <div style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            height: "100%",
            color: "var(--text-tertiary)",
            fontSize: 14,
          }}>
            <div style={{ textAlign: "center" }}>
              {/* Subtle monochrome icon */}
              <svg width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.3, marginBottom: 16 }}>
                <rect x="8" y="8" width="32" height="32" rx="4" />
                <path d="M16 16h16M16 24h10M16 32h12" />
              </svg>
              <div style={{ color: "var(--text-secondary)", fontWeight: 500, fontSize: 13 }}>Open a file to begin</div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Tab Component ─────────────────────────────────────────────────────────

function Tab({
  file,
  isActive,
}: {
  file: { path: string; name: string; isDirty: boolean };
  isActive: boolean;
}) {
  const { setActiveFile, closeFile } = useAppStore();

  return (
    <div
      role="tab"
      aria-selected={isActive}
      tabIndex={isActive ? 0 : -1}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          setActiveFile(file.path);
        } else if (e.key === "Delete") {
          closeFile(file.path);
        }
      }}
      onClick={() => setActiveFile(file.path)}
      style={{
        padding: "6px 14px",
        fontSize: 12,
        cursor: "pointer",
        background: isActive ? "var(--bg-tertiary)" : "transparent",
        color: isActive ? "var(--text-primary)" : "var(--text-tertiary)",
        display: "flex",
        alignItems: "center",
        gap: 6,
        whiteSpace: "nowrap",
        borderRight: "1px solid var(--border-subtle)",
        borderBottom: isActive ? "2px solid var(--accent)" : "2px solid transparent",
        transition: "all 0.1s",
      }}
      onMouseEnter={(e) => { if (!isActive) e.currentTarget.style.background = "var(--bg-hover)"; }}
      onMouseLeave={(e) => { if (!isActive) e.currentTarget.style.background = "transparent"; }}
    >
      <span>{file.name}</span>
      {file.isDirty && (
        <span style={{ color: "var(--warning)", fontSize: 10 }}>●</span>
      )}
      <span
        onClick={(e) => {
          e.stopPropagation();
          closeFile(file.path);
        }}
        style={{
          fontSize: 10,
          color: "var(--text-tertiary)",
          cursor: "pointer",
          padding: "0 3px",
          borderRadius: "var(--radius-xs)",
        }}
        onMouseEnter={(e) => e.currentTarget.style.color = "var(--error)"}
        onMouseLeave={(e) => e.currentTarget.style.color = "var(--text-tertiary)"}
      >
        ×
      </span>
    </div>
  );
}

// ── Monaco Editor Wrapper ─────────────────────────────────────────────────

function MonacoEditor({
  content,
  language,
  theme,
  filePath,
  _isDirty,
}: {
  content: string;
  language: string;
  theme: string;
  filePath: string;
  _isDirty: boolean;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<any>(null);
  const monacoRef = useRef<any>(null);
  const [loading, setLoading] = useState(true);
  const pendingContentRef = useRef<string | null>(null);
  const pendingFilePathRef = useRef<string | null>(null);
  const contentTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Track open tabs for editorContext
  const { openFiles } = useAppStore();
  useEffect(() => {
    editorContext.updateOpenTabs(
      openFiles.map((f) => ({ path: f.path, name: f.name })),
    );
  }, [openFiles]);

  // Initialize Monaco editor once on mount
  useEffect(() => {
    let disposed = false;

    async function loadMonaco() {
      const monaco = await import("monaco-editor");

      if (disposed || !containerRef.current) return;
      monacoRef.current = monaco;

      // Create editor instance
      const editor = monaco.editor.create(containerRef.current, {
        value: content,
        language: mapLanguage(language),
        theme,
        automaticLayout: true,
        fontSize: 13,
        fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
        fontLigatures: true,
        lineNumbers: "on",
        minimap: { enabled: true, scale: 1 },
        scrollBeyondLastLine: false,
        wordWrap: "off",
        tabSize: 2,
        renderWhitespace: "selection",
        bracketPairColorization: { enabled: true },
        padding: { top: 8 },
        smoothScrolling: true,
        cursorBlinking: "smooth",
        cursorSmoothCaretAnimation: "on",
        inlineSuggest: { enabled: true },
      });

      editorRef.current = editor;

      // Apply any pending content that arrived while Monaco was loading
      if (pendingContentRef.current !== null) {
        editor.setValue(pendingContentRef.current);
        editorContext.updateActiveFile(pendingFilePathRef.current || filePath, pendingContentRef.current, language);
        pendingContentRef.current = null;
        pendingFilePathRef.current = null;
      }

      // ── Wire editor context ────────────────────────────────────────────

      // Track cursor and selection changes
      editor.onDidChangeCursorSelection((_e: any) => {
        const pos = editor.getPosition();
        const sel = editor.getSelection();
        if (!pos) return;
        editorContext.updateCursorAndSelection(
          { line: pos.lineNumber, column: pos.column },
          sel && !sel.isEmpty()
            ? {
                startLine: sel.startLineNumber,
                startColumn: sel.startColumn,
                endLine: sel.endLineNumber,
                endColumn: sel.endColumn,
              }
            : null,
        );
      });

      // Track content changes (debounced for completion context)
      editor.onDidChangeModelContent(() => {
        if (contentTimerRef.current) clearTimeout(contentTimerRef.current);
        contentTimerRef.current = setTimeout(() => {
          editorContext.updateContent(editor.getValue());
        }, 50);
      });

      // Set initial context
      editorContext.updateActiveFile(filePath, content, language);
      const initPos = editor.getPosition();
      if (initPos) {
        editorContext.updateCursorAndSelection(
          { line: initPos.lineNumber, column: initPos.column },
          null,
        );
      }

      setLoading(false);
    }

    loadMonaco();

    return () => {
      disposed = true;
      cancelCompletion();
      if (contentTimerRef.current) clearTimeout(contentTimerRef.current);
      editorRef.current?.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Update content when it changes externally
  useEffect(() => {
    const editor = editorRef.current;
    if (!editor) {
      pendingContentRef.current = content;
      pendingFilePathRef.current = filePath;
      return;
    }
    if (editor.getValue() !== content) {
      const cursor = editor.getPosition();
      const selection = editor.getSelection();
      editor.setValue(content);
      if (cursor) {
        editor.setPosition(cursor);
      }
      if (selection) {
        editor.setSelection(selection);
      }
      editorContext.updateActiveFile(filePath, content, language);
    }
  }, [content, filePath, language]);

  // Update active file context when filePath changes
  useEffect(() => {
    editorContext.updateActiveFile(filePath, content, language);
    const editor = editorRef.current;
    if (editor) {
      const pos = editor.getPosition();
      if (pos) {
        editorContext.updateCursorAndSelection(
          { line: pos.lineNumber, column: pos.column },
          null,
        );
      }
    }
  }, [filePath, content, language]);

  // Update theme when it changes
  useEffect(() => {
    if (monacoRef.current) {
      monacoRef.current.editor.setTheme(theme);
    }
  }, [theme]);

  // Register inline completion provider (once)
  useEffect(() => {
    const monaco = monacoRef.current;
    if (!monaco) return;

    const provider = monaco.languages.registerInlineCompletionsProvider("*", {
      provideInlineCompletions: async (model: any, position: any, _context: any, token: any) => {
        const prefix = model.getValueInRange({
          startLineNumber: 1,
          startColumn: 1,
          endLineNumber: position.lineNumber,
          endColumn: position.column,
        });
        const suffix = model.getValueInRange({
          startLineNumber: position.lineNumber,
          startColumn: position.column,
          endLineNumber: model.getLineCount(),
          endColumn: model.getLineMaxColumn(model.getLineCount()),
        });

        const lang = model.getLanguageId();

        const result = await getCompletion(prefix, suffix, lang);
        if (!result || token.isCancellationRequested) {
          return { items: [] };
        }

        return {
          items: [
            {
              insertText: result,
              range: new monaco.Range(
                position.lineNumber,
                position.column,
                position.lineNumber,
                position.column,
              ),
            },
          ],
        };
      },
      freeInlineCompletions: () => {},
    });

    return () => provider.dispose();
  }, [loading]);

  // CodeLens: show agent action buttons on selected code
  useEffect(() => {
    const monaco = monacoRef.current;
    if (!monaco) return;

    const provider = monaco.languages.registerCodeLensProvider("*", {
      provideCodeLenses: (_model: any) => {
        const state = editorContext.getState();
        const selection = state.selection;
        if (!selection) return { lenses: [], dispose: () => {} };

        const range = new monaco.Range(selection.startLine, selection.startColumn, selection.endLine, selection.endColumn);

        return {
          lenses: [
            {
              range,
              id: "agent.explain",
              command: {
                id: "agent.explain",
                title: "  Explain  ",
              },
            },
            {
              range,
              id: "agent.fix",
              command: {
                id: "agent.fix",
                title: "  Fix  ",
              },
            },
            {
              range,
              id: "agent.improve",
              command: {
                id: "agent.improve",
                title: "  Improve  ",
              },
            },
          ],
          dispose: () => {},
        };
      },
      resolveCodeLens: (_model: any, codeLens: any) => codeLens,
    });

    return () => provider.dispose();
  }, [loading]);

  // Git blame inline decorator
  useEffect(() => {
    const monaco = monacoRef.current;
    if (!monaco || !filePath || !editorRef.current) return;

    const decorator = editorRef.current.createDecorationsCollection([]);

    const updateBlame = async () => {
      try {
        const { gitLogFile } = await import("../../services/gitService");
        const blame = await gitLogFile(filePath);
        if (!blame || !editorRef.current) return;

        const decorations = blame.map((entry) => ({
          range: new monaco.Range(entry.line, 1, entry.line, 1),
          options: {
            isWholeLine: true,
            after: {
              content: `  ${entry.author} · ${entry.date}`,
              inlineClassName: "git-blame-inline",
            },
          },
        }));

        decorator.set(decorations);
      } catch {
        // file not in git or git not available — skip silently
      }
    };

    updateBlame();

    return () => {
      decorator.clear();
    };
  }, [filePath, loading]);

  return (
    <div style={{ position: "relative", height: "100%" }}>
      {loading && (
        <div
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          background: theme === "vs-dark" ? "var(--bg-primary)" : "var(--bg-primary)",
          color: "var(--text-tertiary)",
            fontSize: 12,
            zIndex: 1,
          }}
        >
          Loading editor...
        </div>
      )}
      <div ref={containerRef} style={{ height: "100%" }} />
    </div>
  );
}

/** Map file extensions to Monaco language IDs. */
function mapLanguage(lang: string): string {
  const map: Record<string, string> = {
    typescript: "typescript",
    javascript: "javascript",
    ts: "typescript",
    js: "javascript",
    jsx: "javascript",
    tsx: "typescript",
    python: "python",
    py: "python",
    rust: "rust",
    rs: "rust",
    json: "json",
    html: "html",
    css: "css",
    scss: "scss",
    markdown: "markdown",
    md: "markdown",
    yaml: "yaml",
    yml: "yaml",
    toml: "toml",
    xml: "xml",
    sql: "sql",
    sh: "shell",
    bash: "shell",
    zsh: "shell",
    go: "go",
    java: "java",
    c: "c",
    cpp: "cpp",
    "c++": "cpp",
    "objective-c": "objective-c",
    swift: "swift",
    kotlin: "kotlin",
    ruby: "ruby",
    php: "php",
    dockerfile: "dockerfile",
    makefile: "makefile",
  };
  return map[lang] || "plaintext";
}
