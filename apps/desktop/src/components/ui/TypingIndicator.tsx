import React from "react";
import animStyles from "../../styles/animations.module.css";

export function TypingIndicator() {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4, padding: "8px 0" }}>
      <span className={animStyles.typingDot} />
      <span className={animStyles.typingDot} />
      <span className={animStyles.typingDot} />
    </div>
  );
}
