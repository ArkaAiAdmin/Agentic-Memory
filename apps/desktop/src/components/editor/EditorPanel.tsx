import React, { useRef, useEffect, useState } from "react";
import { useAppStore } from "../../stores/appStore";

export function EditorPanel() {
  const { openFiles, activeFile, theme } = useAppStore();

  const activeFileData = openFiles.find((f) => f.path === activeFile);

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      {/* Tab bar */}
      <div
        style={{
          display: "flex",
          background: "#16213e",
          borderBottom: "1px solid #2a2a4a",
          overflow: "auto",
        }}
      >
        {openFiles.map((file) => (
          <Tab
            key={file.path}
            file={file}
            isActive={file.path === activeFile}
          />
        ))}
      </div>

      {/* Editor area */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {activeFileData ? (
          <MonacoEditor
            content={activeFileData.content}
            language={activeFileData.language}
            theme={theme === "dark" ? "vs-dark" : "vs"}
          />
        ) : (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              height: "100%",
              color: "#444",
              fontSize: 14,
            }}
          >
            <div style={{ textAlign: "center" }}>
              <div style={{ fontSize: 48, marginBottom: 16 }}>🧠</div>
              <div>Memory-First Agent IDE</div>
              <div style={{ fontSize: 12, color: "#555", marginTop: 8 }}>
                Open a file or start a conversation
              </div>
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
      onClick={() => setActiveFile(file.path)}
      style={{
        padding: "6px 12px",
        fontSize: 12,
        cursor: "pointer",
        borderRight: "1px solid #2a2a4a",
        background: isActive ? "#1a1a2e" : "transparent",
        color: isActive ? "#fff" : "#888",
        display: "flex",
        alignItems: "center",
        gap: 6,
        whiteSpace: "nowrap",
      }}
    >
      <span>{file.name}</span>
      {file.isDirty && (
        <span style={{ color: "#ff9800", fontSize: 10 }}>●</span>
      )}
      <span
        onClick={(e) => {
          e.stopPropagation();
          closeFile(file.path);
        }}
        style={{
          fontSize: 10,
          color: "#666",
          cursor: "pointer",
          padding: "0 2px",
        }}
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
}: {
  content: string;
  language: string;
  theme: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let disposed = false;

    async function loadMonaco() {
      const monaco = await import("monaco-editor");

      if (disposed || !containerRef.current) return;

      // Create editor instance
      editorRef.current = monaco.editor.create(containerRef.current, {
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
      });

      setLoading(false);
    }

    loadMonaco();

    return () => {
      disposed = true;
      editorRef.current?.dispose();
    };
  }, []);

  // Update content when it changes externally
  useEffect(() => {
    const editor = editorRef.current;
    if (editor && editor.getValue() !== content) {
      editor.setValue(content);
    }
  }, [content]);

  // Update theme when it changes
  useEffect(() => {
    const monaco = (window as any).monaco;
    if (monaco) {
      monaco.editor.setTheme(theme);
    }
  }, [theme]);

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
            background: theme === "vs-dark" ? "#1e1e1e" : "#fff",
            color: "#666",
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
