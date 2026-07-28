import React, { useState, useRef, useEffect, useCallback } from "react";

type Direction = "horizontal" | "vertical";

interface ResizablePaneProps {
  direction?: Direction;
  defaultSize?: number;
  minSize?: number;
  maxSize?: number;
  initialCollapsed?: boolean;
  collapsedSize?: number;
  onResize?: (size: number) => void;
  onToggleCollapse?: (collapsed: boolean) => void;
  children: [React.ReactNode, React.ReactNode];
}

export function ResizablePane({
  direction = "horizontal",
  defaultSize = 300,
  minSize = 150,
  maxSize = 800,
  initialCollapsed = false,
  collapsedSize = 0,
  onResize,
  onToggleCollapse,
  children,
}: ResizablePaneProps) {
  const [size, setSize] = useState(() => (initialCollapsed ? collapsedSize : defaultSize));
  const [isCollapsed, setIsCollapsed] = useState(initialCollapsed);
  const [isDragging, setIsDragging] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const startPosRef = useRef(0);
  const startSizeRef = useRef(0);

  const isHorizontal = direction === "horizontal";

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    startPosRef.current = isHorizontal ? e.clientX : e.clientY;
    startSizeRef.current = size;
    setIsDragging(true);
  }, [isHorizontal, size]);

  useEffect(() => {
    if (!isDragging) return;

    const handleMouseMove = (e: MouseEvent) => {
      const currentPos = isHorizontal ? e.clientX : e.clientY;
      const delta = currentPos - startPosRef.current;
      const newSize = Math.min(maxSize, Math.max(minSize, startSizeRef.current + delta));
      setSize(newSize);
      onResize?.(newSize);
    };

    const handleMouseUp = () => {
      setIsDragging(false);
    };

    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);

    return () => {
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isDragging, isHorizontal, minSize, maxSize, onResize]);

  const toggleCollapse = () => {
    const next = !isCollapsed;
    setIsCollapsed(next);
    setSize(next ? collapsedSize : defaultSize);
    onToggleCollapse?.(next);
  };

  const canCollapse = collapsedSize < size && onToggleCollapse !== undefined;

  const containerStyle: React.CSSProperties = {
    display: "flex",
    flexDirection: isHorizontal ? "row" : "column",
    height: isHorizontal ? "100%" : "auto",
    width: isHorizontal ? (isCollapsed ? collapsedSize : size) : "100%",
    minWidth: isHorizontal ? minSize : 0,
    maxWidth: isHorizontal ? maxSize : "none",
    minHeight: isHorizontal ? 0 : minSize,
    maxHeight: isHorizontal ? "none" : maxSize,
    overflow: "hidden",
    userSelect: isDragging ? "none" : "auto",
    flexShrink: 0,
  };

  const firstChildStyle: React.CSSProperties = {
    flex: "0 0 auto",
    width: isHorizontal ? (isCollapsed ? collapsedSize : size) : "100%",
    height: isHorizontal ? "100%" : (isCollapsed ? collapsedSize : size),
    overflow: "hidden",
    position: "relative",
    transition: isDragging ? "none" : "width 0.15s ease, height 0.15s ease",
  };

  const dividerStyle: React.CSSProperties = {
    flex: "0 0 auto",
    width: isHorizontal ? 4 : "100%",
    height: isHorizontal ? "100%" : 4,
    background: isDragging ? "#4a9eff" : "#2a2a4a",
    cursor: isHorizontal ? "col-resize" : "row-resize",
    position: "relative",
    zIndex: 10,
    transition: isDragging ? "none" : "background 0.15s ease",
  };

  const secondChildStyle: React.CSSProperties = {
    flex: 1,
    minWidth: 0,
    minHeight: 0,
    overflow: "hidden",
  };

  const collapseButtonStyle: React.CSSProperties = {
    position: "absolute",
    top: 8,
    [isHorizontal ? "right" : "bottom"]: 4,
    width: 20,
    height: 20,
    borderRadius: 4,
    border: "1px solid #2a2a4a",
    background: "#16213e",
    color: "#888",
    fontSize: 10,
    cursor: "pointer",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 20,
    padding: 0,
    lineHeight: 1,
  };

  return (
    <div ref={containerRef} style={containerStyle}>
      <div style={firstChildStyle}>
        {children[0]}
        {canCollapse && !isCollapsed && (
          <button onClick={toggleCollapse} style={collapseButtonStyle} title={isCollapsed ? "Expand" : "Collapse"}>
            {isHorizontal ? "‹" : "›"}
          </button>
        )}
        {canCollapse && isCollapsed && (
          <button onClick={toggleCollapse} style={collapseButtonStyle} title="Expand">
            {isHorizontal ? "›" : "‹"}
          </button>
        )}
      </div>
      <div style={dividerStyle} onMouseDown={handleMouseDown} />
      <div style={secondChildStyle}>{children[1]}</div>
    </div>
  );
}
