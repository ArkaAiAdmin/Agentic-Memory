import React from "react";
import styles from "../../styles/components.module.css";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "icon";
type Size = "sm" | "md" | "lg";

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: React.ReactNode;
}

const variantMap: Record<Variant, string> = {
  primary: styles.btnPrimary,
  secondary: styles.btnSecondary,
  ghost: styles.btnGhost,
  danger: styles.btnDanger,
  icon: styles.btnIcon,
};

const sizeMap: Record<Size, string> = {
  sm: styles.btnSm,
  md: styles.btnMd,
  lg: styles.btnLg,
};

export function Button({
  variant = "primary",
  size = "md",
  icon,
  children,
  className,
  ...props
}: ButtonProps) {
  if (variant === "icon") {
    return (
      <button
        className={`${styles.btnIcon} ${className ?? ""}`}
        {...props}
      >
        {icon ?? children}
      </button>
    );
  }

  return (
    <button
      className={`${styles.btn} ${variantMap[variant]} ${sizeMap[size]} ${className ?? ""}`}
      {...props}
    >
      {icon}
      {children}
    </button>
  );
}
