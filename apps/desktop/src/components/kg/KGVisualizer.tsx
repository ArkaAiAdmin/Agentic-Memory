import React, { useRef, useState, useCallback } from "react";
import { useAppStore } from "../../stores/appStore";
import type { KGNode } from "@ami/shared";

interface PositionedNode {
  node: KGNode;
  x: number;
  y: number;
  vx: number;
  vy: number;
}

interface Edge {
  from: { x: number; y: number };
  to: { x: number; y: number };
  label: string;
  confidence: number;
  key: string;
}

const REPULSION = 800;
const ATTRACTION = 0.01;
const DAMPING = 0.85;
const CENTER_GRAVITY = 0.005;
const ITERATIONS = 80;

function buildEdges(nodes: PositionedNode[]): Edge[] {
  const edges: Edge[] = [];
  for (const { node, x, y } of nodes) {
    for (const fact of node.facts) {
      const target = nodes.find((n) => n.node.name === fact.object);
      if (target) {
        edges.push({
          from: { x, y },
          to: { x: target.x, y: target.y },
          label: fact.predicate,
          confidence: fact.confidence,
          key: `${node.id}-${fact.predicate}-${target.node.id}`,
        });
      }
    }
  }
  return edges;
}

function forceLayout(nodes: KGNode[]): { positioned: PositionedNode[]; edges: Edge[] } {
  const width = 600;
  const height = 400;
  const cx = width / 2;
  const cy = height / 2;

  const positioned: PositionedNode[] = nodes.map((node, i) => {
    const angle = (2 * Math.PI * i) / nodes.length;
    const r = Math.min(width, height) / 4;
    return {
      node,
      x: cx + r * Math.cos(angle),
      y: cy + r * Math.sin(angle),
      vx: 0,
      vy: 0,
    };
  });

  for (let iter = 0; iter < ITERATIONS; iter++) {
    const alpha = 1 - iter / ITERATIONS;

    for (let i = 0; i < positioned.length; i++) {
      for (let j = i + 1; j < positioned.length; j++) {
        const dx = positioned[j].x - positioned[i].x;
        const dy = positioned[j].y - positioned[i].y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = REPULSION * alpha / (dist * dist);
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        positioned[i].vx -= fx;
        positioned[i].vy -= fy;
        positioned[j].vx += fx;
        positioned[j].vy += fy;
      }
    }

    const edges = buildEdges(positioned);
    for (const edge of edges) {
      const dx = edge.to.x - edge.from.x;
      const dy = edge.to.y - edge.from.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = ATTRACTION * dist * alpha;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      const fromIdx = positioned.findIndex((n) => n.node.id === edge.key.split("-")[0]);
      const toIdx = positioned.findIndex((n) => n.node.id === edge.key.split("-")[2]);
      if (fromIdx >= 0) {
        positioned[fromIdx].vx += fx;
        positioned[fromIdx].vy += fy;
      }
      if (toIdx >= 0) {
        positioned[toIdx].vx -= fx;
        positioned[toIdx].vy -= fy;
      }
    }

    for (const p of positioned) {
      p.vx += (cx - p.x) * CENTER_GRAVITY * alpha;
      p.vy += (cy - p.y) * CENTER_GRAVITY * alpha;
      p.vx *= DAMPING;
      p.vy *= DAMPING;
      p.x += p.vx;
      p.y += p.vy;
      p.x = Math.max(20, Math.min(width - 20, p.x));
      p.y = Math.max(20, Math.min(height - 20, p.y));
    }
  }

  return { positioned, edges: buildEdges(positioned) };
}

export function KGVisualizer() {
  const { kgNodes } = useAppStore();
  const svgRef = useRef<SVGSVGElement>(null);
  const [tooltip, setTooltip] = useState<{ node: KGNode; x: number; y: number } | null>(null);
  const [hoveredNodeId, setHoveredNodeId] = useState<string | null>(null);
  const [transform, setTransform] = useState({ scale: 1, x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState(false);
  const panStart = useRef({ x: 0, y: 0, tx: 0, ty: 0 });

  const { positioned, edges } = React.useMemo(() => {
    if (kgNodes.length === 0) return { positioned: [], edges: [] };
    return forceLayout(kgNodes);
  }, [kgNodes]);

  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    setTransform((prev) => {
      const delta = -e.deltaY * 0.001;
      const newScale = Math.min(3, Math.max(0.3, prev.scale + delta));
      return { ...prev, scale: newScale };
    });
  }, []);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button === 0) {
      setIsPanning(true);
      panStart.current = { x: e.clientX, y: e.clientY, tx: transform.x, ty: transform.y };
    }
  }, [transform]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (isPanning) {
      const dx = e.clientX - panStart.current.x;
      const dy = e.clientY - panStart.current.y;
      setTransform((prev) => ({
        ...prev,
        x: panStart.current.tx + dx,
        y: panStart.current.ty + dy,
      }));
    }
  }, [isPanning]);

  const handleMouseUp = useCallback(() => {
    setIsPanning(false);
  }, []);

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

  const width = 600;
  const height = 400;

  return (
    <div style={{ padding: 8, height: "100%", display: "flex", flexDirection: "column" }}>
      <div
        style={{
          fontSize: 11,
          fontWeight: 600,
          color: "#888",
          marginBottom: 8,
          display: "flex",
          justifyContent: "space-between",
        }}
      >
        <span>Knowledge Graph ({kgNodes.length} entities, {edges.length} edges)</span>
        <span style={{ fontWeight: 400 }}>Scroll to zoom · Drag to pan</span>
      </div>
      <div style={{ flex: 1, overflow: "hidden", borderRadius: 6, cursor: isPanning ? "grabbing" : "grab" }}>
        <svg
          ref={svgRef}
          width={width}
          height={height}
          viewBox={`0 0 ${width} ${height}`}
          style={{ background: "#0d1117", borderRadius: 6 }}
          onWheel={handleWheel}
          onMouseDown={handleMouseDown}
          onMouseMove={handleMouseMove}
          onMouseUp={handleMouseUp}
          onMouseLeave={handleMouseUp}
        >
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

          <g transform={`translate(${transform.x} ${transform.y}) scale(${transform.scale})`}>
            {edges.map((edge) => (
              <g key={edge.key}>
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

            {positioned.map(({ node, x, y }) => (
              <g
                key={node.id}
                onClick={() => setTooltip((prev) => (prev?.node.id === node.id ? null : { node, x, y }))}
                onMouseEnter={() => setHoveredNodeId(node.id)}
                onMouseLeave={() => setHoveredNodeId(null)}
                style={{ cursor: "pointer" }}
              >
                <circle
                  cx={x}
                  cy={y}
                  r={12}
                  fill="#1a1a2e"
                  stroke="#82aaff"
                  strokeWidth={1.5}
                  opacity={hoveredNodeId === node.id ? 0.7 : 1}
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
          </g>
        </svg>
      </div>

      {tooltip && (
        <div
          style={{
            position: "absolute",
            left: tooltip.x + 20,
            top: tooltip.y - 10,
            background: "#1e1e2e",
            border: "1px solid #2a2a4a",
            borderRadius: 8,
            padding: "8px 12px",
            fontSize: 11,
            color: "#c0c0c0",
            maxWidth: 250,
            boxShadow: "0 4px 16px rgba(0,0,0,0.3)",
            zIndex: 50,
          }}
        >
          <div style={{ fontWeight: 600, color: "#82aaff", marginBottom: 4 }}>
            {tooltip.node.name}
          </div>
          <div style={{ fontSize: 10, color: "#666" }}>
            Type: {tooltip.node.type}
          </div>
          <div style={{ fontSize: 10, color: "#666" }}>
            Facts: {tooltip.node.facts.length}
          </div>
          {tooltip.node.facts.length > 0 && (
            <div style={{ marginTop: 4 }}>
              {tooltip.node.facts.slice(0, 5).map((f, fi) => (
                <div key={fi} style={{ fontSize: 10, color: "#888" }}>
                  {f.predicate} → {f.object}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
