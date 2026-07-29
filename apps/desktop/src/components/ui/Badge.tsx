import React from "react";
import styles from "../../styles/components.module.css";

type BadgeVariant = "success" | "warning" | "error" | "info" | "neutral";

interface BadgeProps {
  variant?: BadgeVariant;
  dot?: boolean;
  children: React.ReactNode;
  className?: string;
}

const variantMap: Record<BadgeVariant, string> = {
  success: styles.badgeSuccess,
  warning: styles.badgeWarning,
  error: styles.badgeError,
  info: styles.badgeInfo,
  neutral: styles.badgeNeutral,
};

const dotMap: Record<BadgeVariant, string> = {
  success: styles.dotGreen,
  warning: styles.dotYellow,
  error: styles.dotRed,
  info: styles.dotGreen,
  neutral: styles.dotGray,
};

export function Badge({ variant = "neutral", dot = false, children, className }: BadgeProps) {
  return (
    <span className={`${styles.badge} ${variantMap[variant]} ${className ?? ""}`}>
      {dot && <span className={`${styles.dot} ${dotMap[variant]}`} />}
      {children}
    </span>
  );
}
