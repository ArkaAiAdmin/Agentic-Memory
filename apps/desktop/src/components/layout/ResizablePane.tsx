import React, { useRef, useCallback, useState } from "react";

/**
 * ResizablePane
 *
 * A single fixed-size pane with a draggable handle on one edge. The neighbouring
 * content is a *sibling* that flexes to fill the remaining space, so this pane
 * only ever owns its own width/height.
 *
 * Size is **controlled**: the parent owns `size` and updates it via `onResize`
 * (typically persisted in the app store). This fixes two bugs in the previous
 * design where the container width equalled the pane size (clipping its own
 * divider) and where collapse was a one-way trip.
 *
 * - `side` is the edge that carries the drag handle:
 *     - left sidebar  → `side="right"`
 *     - right panel   → `side="left"`
 *     - bottom panel  → `side="top"`
 *     - top panel     → `side="bottom"`
 * - Double-click the handle to reset to `defaultSize`.
 */

type Side = "left" | "right" | "top" | "bottom";

interface ResizablePaneProps {
  side: Side;
  size: number;
  onResize: (size: number) => void;
  minSize?: number;
  maxSize?: number;
  defaultSize?: number;
  style?: React.CSSProperties;
  children: React.ReactNode;
}

export function ResizablePane({
  side,
  size,
  onResize,
  minSize = 150,
  maxSize = 900,
  defaultSize,
  style,
  children,
}: ResizablePaneProps) {
  const isHorizontal = side === "left" || side === "right";
  const [isDragging, setIsDragging] = useState(false);
  const startPosRef = useRef(0);
  const startSizeRef = useRef(0);

  const clamp = useCallback(
    (v: number) => Math.min(maxSize, Math.max(minSize, v)),
    [minSize, maxSize],
  );

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      startPosRef.current = isHorizontal ? e.clientX : e.clientY;
      startSizeRef.current = size;
      setIsDragging(true);

      const onMove = (ev: MouseEvent) => {
        const currentPos = isHorizontal ? ev.clientX : ev.clientY;
        const rawDelta = currentPos - startPosRef.current;
        // `left`/`top` handles grow the pane when dragged *away* from the pane.
        const delta = side === "left" || side === "top" ? -rawDelta : rawDelta;
        onResize(clamp(startSizeRef.current + delta));
      };

      const onUp = () => {
        setIsDragging(false);
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [isHorizontal, side, size, onResize, clamp],
  );

  const handleDoubleClick = useCallback(() => {
    if (defaultSize != null) onResize(clamp(defaultSize));
  }, [defaultSize, onResize, clamp]);

  const containerStyle: React.CSSProperties = {
    position: "relative",
    flexShrink: 0,
    overflow: "hidden",
    ...(isHorizontal
      ? { width: size, height: "100%" }
      : { height: size, width: "100%" }),
    ...style,
  };

  // Absolutely-positioned handle straddling the chosen edge — never clipped,
  // with a generous hit area even though it renders as a thin line.
  const handleThickness = 5;
  const handleStyle: React.CSSProperties = {
    position: "absolute",
    zIndex: 20,
    background: isDragging ? "#4a9eff" : "transparent",
    transition: isDragging ? "none" : "background 0.15s ease",
    ...(isHorizontal
      ? {
          top: 0,
          bottom: 0,
          width: handleThickness,
          cursor: "col-resize",
          [side]: -Math.floor(handleThickness / 2),
        }
      : {
          left: 0,
          right: 0,
          height: handleThickness,
          cursor: "row-resize",
          [side]: -Math.floor(handleThickness / 2),
        }),
  };

  return (
    <div style={containerStyle}>
      <div style={{ width: "100%", height: "100%", overflow: "auto" }}>{children}</div>
      <div
        style={handleStyle}
        onMouseDown={handleMouseDown}
        onDoubleClick={handleDoubleClick}
        title="Drag to resize · double-click to reset"
        role="separator"
        aria-label="Resize panel"
        aria-orientation={isHorizontal ? "vertical" : "horizontal"}
        tabIndex={0}
        onKeyDown={(e) => {
          const delta = (e.key === "ArrowRight" || e.key === "ArrowDown") ? 10
            : (e.key === "ArrowLeft" || e.key === "ArrowUp") ? -10 : 0;
          if (delta !== 0) {
            e.preventDefault();
            onResize(clamp(size + delta));
          }
        }}
      />
      {/* Full-screen overlay while dragging so Monaco/xterm don't swallow the
          mousemove events and the resize cursor stays consistent. */}
      {isDragging && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 9999,
            cursor: isHorizontal ? "col-resize" : "row-resize",
          }}
        />
      )}
    </div>
  );
}
