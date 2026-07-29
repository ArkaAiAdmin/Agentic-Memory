import React, { useState, useEffect } from "react";
import { useAppStore } from "../../stores/appStore";
import { memoryBridge } from "@ami/memory-bridge";

/**
 * Skill Browser
 *
 * Shows compiled skills from the memory system.
 * Skills are auto-extracted from lessons and can be invoked by the agent.
 */

interface Skill {
  id: string;
  title: string;
  content: string;
  tags: string[];
  created_at: number;
  usage_count: number;
}

export function SkillBrowser() {
  const { theme } = useAppStore();
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(false);
  const [expandedSkill, setExpandedSkill] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function doLoad() {
      if (!memoryBridge.isRunning) return;
      setLoading(true);
      try {
        const results = await memoryBridge.search({
          query: "skill",
          category: "skill",
          limit: 50,
        });
        if (!cancelled) {
          setSkills(
            results.map((r) => ({
              id: r.note_id,
              title: r.content.split("\n")[0] || r.note_id,
              content: r.content,
              tags: r.tags,
              created_at: r.created_at,
              usage_count: (r.metadata?.usage_count as number) ?? 0,
            })),
          );
        }
      } catch (err) {
        if (!cancelled) {
          console.error("Failed to load skills:", err);
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }
    doLoad();
    return () => { cancelled = true; };
  }, [memoryBridge]);

  const isDark = theme === "dark";
  const bgColor = isDark ? "#161b22" : "#fff";
  const textColor = isDark ? "#c9d1d9" : "#24292f";
  const borderColor = isDark ? "#30363d" : "#d0d7de";

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: bgColor,
        color: textColor,
        fontSize: 12,
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "8px 12px",
          borderBottom: `1px solid ${borderColor}`,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <span style={{ fontWeight: 600 }}>
          Skills ({skills.length})
        </span>
        <button
          onClick={() => {}}
          style={{
            background: "none",
            border: `1px solid ${borderColor}`,
            color: textColor,
            cursor: "pointer",
            padding: "2px 8px",
            borderRadius: 4,
            fontSize: 11,
          }}
        >
          {loading ? "..." : "Refresh"}
        </button>
      </div>

      {/* Skills list */}
      <div style={{ flex: 1, overflow: "auto", padding: "8px 12px" }}>
        {loading && skills.length === 0 ? (
          <div style={{ color: "#666", textAlign: "center", padding: 20 }}>
            Loading skills...
          </div>
        ) : skills.length === 0 ? (
          <div style={{ color: "#666", textAlign: "center", padding: 20 }}>
            <div style={{ fontSize: 24, marginBottom: 8 }}>📚</div>
            <div>No skills compiled yet</div>
            <div style={{ fontSize: 11, marginTop: 4 }}>
              Skills are auto-extracted from lessons
            </div>
          </div>
        ) : (
          skills.map((skill) => (
            <SkillCard
              key={skill.id}
              skill={skill}
              theme={theme}
              isExpanded={expandedSkill === skill.id}
              onToggle={() =>
                setExpandedSkill(
                  expandedSkill === skill.id ? null : skill.id,
                )
              }
            />
          ))
        )}
      </div>
    </div>
  );
}

function SkillCard({
  skill,
  theme,
  isExpanded,
  onToggle,
}: {
  skill: Skill;
  theme: string;
  isExpanded: boolean;
  onToggle: () => void;
}) {
  const isDark = theme === "dark";

  return (
    <div
      style={{
        marginBottom: 8,
        borderRadius: 6,
        border: `1px solid ${isDark ? "#30363d" : "#d0d7de"}`,
        background: isDark ? "#0d1117" : "#f6f8fa",
        overflow: "hidden",
      }}
    >
      {/* Header */}
      <div
        onClick={onToggle}
        style={{
          padding: "8px 10px",
          cursor: "pointer",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ fontSize: 14 }}>⚡</span>
          <span style={{ fontWeight: 500 }}>{skill.title}</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {skill.usage_count > 0 && (
            <span
              style={{
                fontSize: 10,
                color: "#888",
                background: isDark ? "#21262d" : "#e1e4e8",
                padding: "1px 6px",
                borderRadius: 3,
              }}
            >
              {skill.usage_count}x
            </span>
          )}
          <span
            style={{
              fontSize: 10,
              color: "#888",
              transform: isExpanded ? "rotate(90deg)" : "none",
              transition: "transform 0.15s",
            }}
          >
            ▶
          </span>
        </div>
      </div>

      {/* Expanded content */}
      {isExpanded && (
        <div
          style={{
            padding: "0 10px 10px",
            borderTop: `1px solid ${isDark ? "#21262d" : "#e1e4e8"}`,
          }}
        >
          {/* Tags */}
          {skill.tags.length > 0 && (
            <div
              style={{
                display: "flex",
                gap: 4,
                flexWrap: "wrap",
                marginTop: 8,
                marginBottom: 8,
              }}
            >
              {skill.tags.map((tag) => (
                <span
                  key={tag}
                  style={{
                    fontSize: 10,
                    color: "#58a6ff",
                    background: isDark ? "#0d1117" : "#ddf4ff",
                    padding: "1px 6px",
                    borderRadius: 3,
                  }}
                >
                  {tag}
                </span>
              ))}
            </div>
          )}

          {/* Content */}
          <pre
            style={{
              fontSize: 11,
              fontFamily: "'JetBrains Mono', monospace",
              lineHeight: 1.5,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              color: isDark ? "#8b949e" : "#57606a",
              margin: 0,
              maxHeight: 200,
              overflow: "auto",
            }}
          >
            {skill.content}
          </pre>

          {/* Timestamp */}
          <div
            style={{
              fontSize: 10,
              color: "#666",
              marginTop: 8,
            }}
          >
            Created: {new Date(skill.created_at).toLocaleDateString()}
          </div>
        </div>
      )}
    </div>
  );
}
