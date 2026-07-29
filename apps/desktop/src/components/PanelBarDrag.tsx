/**
 * PanelBarDrag — PanelBar with drag-to-resize on both sides.
 *
 * Left handle: drag right to shrink panel, left to grow
 * Right handle: drag right to grow panel, left to shrink
 */

import React, { useRef, useCallback, useState } from "react";
import { PanelBar } from "./PanelBar";

export function PanelBarDrag({ onResize, currentSize }: { onResize: (size: number) => void; currentSize: number }) {
  const [isDragging, setIsDragging] = useState(false);
  const startPosRef = useRef(0);
  const startSizeRef = useRef(0);
  const invertRef = useRef(false);

  const handleMouseDown = useCallback(
    (e: React.MouseEvent, invert: boolean) => {
      e.preventDefault();
      startPosRef.current = e.clientX;
      startSizeRef.current = currentSize;
      invertRef.current = invert;
      setIsDragging(true);

      const onMove = (ev: MouseEvent) => {
        const rawDelta = ev.clientX - startPosRef.current;
        const delta = invertRef.current ? -rawDelta : rawDelta;
        onResize(Math.max(300, Math.min(900, startSizeRef.current + delta)));
      };

      const onUp = () => {
        setIsDragging(false);
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [onResize, currentSize],
  );

  return (
    <div style={{ display: "flex", flexShrink: 0 }}>
      {/* Left handle — drag right to shrink, left to grow */}
      <div
        onMouseDown={(e) => handleMouseDown(e, true)}
        role="separator"
        aria-label="Resize panel"
        aria-orientation="vertical"
        tabIndex={0}
        onKeyDown={(e) => {
          const delta = e.key === "ArrowRight" ? 10 : e.key === "ArrowLeft" ? -10 : 0;
          if (delta !== 0) {
            e.preventDefault();
            onResize(Math.max(300, Math.min(900, currentSize + delta)));
          }
        }}
        style={{
          width: 3,
          cursor: "col-resize",
          background: isDragging ? "var(--accent)" : "transparent",
          transition: isDragging ? "none" : "background 0.15s",
          flexShrink: 0,
        }}
      />
      <PanelBar />
      {/* Right handle — drag right to grow, left to shrink */}
      <div
        onMouseDown={(e) => handleMouseDown(e, false)}
        role="separator"
        aria-label="Resize panel"
        aria-orientation="vertical"
        tabIndex={0}
        onKeyDown={(e) => {
          const delta = e.key === "ArrowRight" ? 10 : e.key === "ArrowLeft" ? -10 : 0;
          if (delta !== 0) {
            e.preventDefault();
            onResize(Math.max(300, Math.min(900, currentSize + delta)));
          }
        }}
        style={{
          width: 3,
          cursor: "col-resize",
          background: isDragging ? "var(--accent)" : "transparent",
          transition: isDragging ? "none" : "background 0.15s",
          flexShrink: 0,
        }}
      />
      {isDragging && (
        <div style={{ position: "fixed", inset: 0, zIndex: 9999, cursor: "col-resize" }} />
      )}
    </div>
  );
}
