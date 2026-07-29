import React from "react";
import styles from "../../styles/components.module.css";

type AvatarSize = "sm" | "md" | "lg";
type AvatarType = "user" | "agent";

interface AvatarProps {
  type?: AvatarType;
  size?: AvatarSize;
  name?: string;
  className?: string;
}

const sizeMap: Record<AvatarSize, string> = {
  sm: styles.avatarSm,
  md: styles.avatarMd,
  lg: styles.avatarLg,
};

const typeMap: Record<AvatarType, string> = {
  user: styles.avatarUser,
  agent: styles.avatarAgent,
};

function getInitials(name: string): string {
  return name.charAt(0).toUpperCase();
}

export function Avatar({ type = "user", size = "md", name = "U", className }: AvatarProps) {
  return (
    <div className={`${styles.avatar} ${sizeMap[size]} ${typeMap[type]} ${className ?? ""}`}>
      {type === "agent" ? (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M12 2a4 4 0 0 1 4 4v2a4 4 0 0 1-8 0V6a4 4 0 0 1 4-4z" />
          <path d="M12 12c-4 0-8 2-8 4v2h16v-2c0-2-4-4-8-4z" />
        </svg>
      ) : (
        getInitials(name)
      )}
    </div>
  );
}
