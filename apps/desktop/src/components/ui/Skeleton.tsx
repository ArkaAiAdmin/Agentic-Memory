import React from "react";
import styles from "../../styles/components.module.css";

interface SkeletonProps {
  variant?: "text" | "title" | "avatar" | "card";
  width?: string;
  height?: string;
  className?: string;
}

const variantMap: Record<string, string> = {
  text: styles.skeletonText,
  title: styles.skeletonTitle,
  avatar: styles.skeletonAvatar,
  card: styles.skeletonCard,
};

export function Skeleton({ variant = "text", width, height, className }: SkeletonProps) {
  return (
    <div
      className={`${styles.skeleton} ${variantMap[variant]} ${className ?? ""}`}
      style={{ width, height }}
    />
  );
}
