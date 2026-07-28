import React, { useState, useCallback, useEffect } from "react";
import { useAppStore } from "../../stores/appStore";
import { memoryBridge } from "@ami/memory-bridge";
import type { BeliefAssertion } from "@ami/shared";

/**
 * Belief Review Panel
 *
 * Dedicated panel for reviewing and updating belief assertions:
 * - Low-confidence beliefs highlighted
 * - Stale beliefs flagged
 * - Agent can update/supersede beliefs
 */

export function BeliefReviewPanel() {
  const { theme } = useAppStore();
  const [beliefs, setBeliefs] = useState<BeliefAssertion[]>([]);
  const [loading, setLoading] = useState(false);
  const [filter, setFilter] = useState<"all" | "low_confidence" | "stale">(
    "all",
  );

  const loadBeliefs = useCallback(async () => {
    if (!memoryBridge.isRunning) return;
    setLoading(true);
    try {
      const result = await memoryBridge.reviewBeliefs({ minConfidence: 0 });
      setBeliefs(result);
    } catch (err) {
      console.error("Failed to load beliefs:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadBeliefs();
  }, [loadBeliefs]);

  const filteredBeliefs = beliefs.filter((b) => {
    if (filter === "low_confidence") return b.confidence < 0.5;
    if (filter === "stale") {
      const thirtyDaysAgo = Date.now() - 30 * 24 * 60 * 60 * 1000;
      return b.created_at < thirtyDaysAgo;
    }
    return true;
  });

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
          Belief Assertions ({filteredBeliefs.length})
        </span>
        <button
          onClick={loadBeliefs}
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

      {/* Filter tabs */}
      <div
        style={{
          display: "flex",
          padding: "4px 12px",
          gap: 4,
          borderBottom: `1px solid ${borderColor}`,
        }}
      >
        {(["all", "low_confidence", "stale"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            style={{
              background: filter === f ? (isDark ? "#30363d" : "#e1e4e8") : "none",
              border: "none",
              color: filter === f ? textColor : "#888",
              cursor: "pointer",
              padding: "2px 8px",
              borderRadius: 4,
              fontSize: 11,
            }}
          >
            {f === "all"
              ? "All"
              : f === "low_confidence"
                ? "Low Confidence"
                : "Stale"}
          </button>
        ))}
      </div>

      {/* Beliefs list */}
      <div style={{ flex: 1, overflow: "auto", padding: "8px 12px" }}>
        {loading && beliefs.length === 0 ? (
          <div style={{ color: "#666", textAlign: "center", padding: 20 }}>
            Loading beliefs...
          </div>
        ) : filteredBeliefs.length === 0 ? (
          <div style={{ color: "#666", textAlign: "center", padding: 20 }}>
            No beliefs match the filter
          </div>
        ) : (
          filteredBeliefs.map((belief) => (
            <BeliefCard key={belief.id} belief={belief} theme={theme} />
          ))
        )}
      </div>
    </div>
  );
}

function BeliefCard({
  belief,
  theme,
}: {
  belief: BeliefAssertion;
  theme: string;
}) {
  const isDark = theme === "dark";
  const isLowConfidence = belief.confidence < 0.5;
  const thirtyDaysAgo = Date.now() - 30 * 24 * 60 * 60 * 1000;
  const isStale = belief.created_at < thirtyDaysAgo;
  const isSuperseded = !!belief.superseded_by;

  const borderColor = isSuperseded
    ? "#f85149"
    : isLowConfidence
      ? "#d29922"
      : isStale
        ? "#8b949e"
        : isDark
          ? "#30363d"
          : "#d0d7de";

  const confidenceColor =
    belief.confidence >= 0.8
      ? "#3fb950"
      : belief.confidence >= 0.5
        ? "#d29922"
        : "#f85149";

  return (
    <div
      style={{
        padding: 10,
        marginBottom: 8,
        borderRadius: 6,
        border: `1px solid ${borderColor}`,
        background: isDark ? "#0d1117" : "#f6f8fa",
      }}
    >
      {/* Status badges */}
      <div style={{ display: "flex", gap: 4, marginBottom: 6 }}>
        {isLowConfidence && (
          <span
            style={{
              background: "#d29922",
              color: "#000",
              padding: "1px 6px",
              borderRadius: 3,
              fontSize: 10,
              fontWeight: 600,
            }}
          >
            LOW CONFIDENCE
          </span>
        )}
        {isStale && (
          <span
            style={{
              background: "#8b949e",
              color: "#000",
              padding: "1px 6px",
              borderRadius: 3,
              fontSize: 10,
              fontWeight: 600,
            }}
          >
            STALE
          </span>
        )}
        {isSuperseded && (
          <span
            style={{
              background: "#f85149",
              color: "#fff",
              padding: "1px 6px",
              borderRadius: 3,
              fontSize: 10,
              fontWeight: 600,
            }}
          >
            SUPERSEDED
          </span>
        )}
      </div>

      {/* Content */}
      <div
        style={{
          fontSize: 12,
          lineHeight: 1.5,
          marginBottom: 8,
          opacity: isSuperseded ? 0.6 : 1,
        }}
      >
        {belief.content}
      </div>

      {/* Metadata */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          fontSize: 10,
          color: "#888",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {/* Confidence bar */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 4,
            }}
          >
            <div
              style={{
                width: 60,
                height: 4,
                background: isDark ? "#21262d" : "#d0d7de",
                borderRadius: 2,
                overflow: "hidden",
              }}
            >
              <div
                style={{
                  width: `${belief.confidence * 100}%`,
                  height: "100%",
                  background: confidenceColor,
                  borderRadius: 2,
                }}
              />
            </div>
            <span>{(belief.confidence * 100).toFixed(0)}%</span>
          </div>
          <span>Evidence: {belief.evidence_count}</span>
        </div>
        <span>
          {new Date(belief.created_at).toLocaleDateString()}
        </span>
      </div>

      {/* Superseded info */}
      {isSuperseded && (
        <div
          style={{
            marginTop: 6,
            fontSize: 10,
            color: "#f85149",
          }}
        >
          Superseded by: {belief.superseded_by}
        </div>
      )}
    </div>
  );
}
