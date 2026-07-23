import React, { useRef, useEffect } from "react";
import { useAppStore } from "../../stores/appStore";
import type { KGNode, KGEdge } from "@ami/shared";

/**
 * Knowledge Graph Visualizer
 *
 * Renders an interactive knowledge graph using SVG.
 * Shows entities as nodes and facts as edges.
 * In a full implementation, this would use D3.js or Cytoscape.js
 * for force-directed layout and interactive exploration.
 */

export function KGVisualizer() {
  const { kgNodes } = useAppStore();
  const svgRef = useRef<SVGSVGElement>(null);

  if (kgNodes.length === 0) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          height: "100%",
          color: "#555",
          fontSize: 13,
        }}
      >
        <div style={{ textAlign: "center" }}>
          <div style={{ fontSize: 32, marginBottom: 8 }}>🔗</div>
          <div>Knowledge Graph</div>
          <div style={{ fontSize: 11, marginTop: 4 }}>
            Entities and facts will appear here
          </div>
        </div>
      </div>
    );
  }

  // Simple circular layout for initial visualization
  const width = 400;
  const height = 300;
  const cx = width / 2;
  const cy = height / 2;
  const radius = Math.min(width, height) / 3;

  const nodePositions = kgNodes.map((node, i) => {
    const angle = (2 * Math.PI * i) / kgNodes.length;
    return {
      node,
      x: cx + radius * Math.cos(angle),
      y: cy + radius * Math.sin(angle),
    };
  });

  // Build edges from facts
  const edges: Array<{
    from: { x: number; y: number };
    to: { x: number; y: number };
    label: string;
    confidence: number;
  }> = [];

  for (const { node, x, y } of nodePositions) {
    for (const fact of node.facts) {
      const target = nodePositions.find(
        (n) => n.node.name === fact.object,
      );
      if (target) {
        edges.push({
          from: { x, y },
          to: { x: target.x, y: target.y },
          label: fact.predicate,
          confidence: fact.confidence,
        });
      }
    }
  }

  return (
    <div style={{ padding: 8 }}>
      <div
        style={{
          fontSize: 11,
          fontWeight: 600,
          color: "#888",
          marginBottom: 8,
        }}
      >
        Knowledge Graph ({kgNodes.length} entities, {edges.length} edges)
      </div>
      <svg
        ref={svgRef}
        width={width}
        height={height}
        style={{ background: "#0d1117", borderRadius: 6 }}
      >
        {/* Edges */}
        {edges.map((edge, i) => (
          <g key={`edge-${i}`}>
            <line
              x1={edge.from.x}
              y1={edge.from.y}
              x2={edge.to.x}
              y2={edge.to.y}
              stroke={`rgba(130, 170, 255, ${edge.confidence})`}
              strokeWidth={1}
              markerEnd="url(#arrowhead)"
            />
            <text
              x={(edge.from.x + edge.to.x) / 2}
              y={(edge.from.y + edge.to.y) / 2 - 4}
              fill="#666"
              fontSize={8}
              textAnchor="middle"
            >
              {edge.label}
            </text>
          </g>
        ))}

        {/* Nodes */}
        {nodePositions.map(({ node, x, y }) => (
          <g key={node.id}>
            <circle
              cx={x}
              cy={y}
              r={12}
              fill="#1a1a2e"
              stroke="#82aaff"
              strokeWidth={1.5}
            />
            <text
              x={x}
              y={y + 20}
              fill="#c0c0c0"
              fontSize={9}
              textAnchor="middle"
            >
              {node.name.length > 15
                ? node.name.slice(0, 15) + "..."
                : node.name}
            </text>
            <text
              x={x}
              y={y + 3}
              fill="#82aaff"
              fontSize={8}
              textAnchor="middle"
            >
              {node.type.charAt(0).toUpperCase()}
            </text>
          </g>
        ))}

        {/* Arrow marker */}
        <defs>
          <marker
            id="arrowhead"
            markerWidth="6"
            markerHeight="4"
            refX="6"
            refY="2"
            orient="auto"
          >
            <polygon points="0 0, 6 2, 0 4" fill="#82aaff" />
          </marker>
        </defs>
      </svg>
    </div>
  );
}
