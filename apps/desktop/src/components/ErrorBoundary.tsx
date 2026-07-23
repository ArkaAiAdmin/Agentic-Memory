import React from "react";

interface Props {
  children: React.ReactNode;
  fallbackLabel?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * Catches render errors in child components so a single broken panel
 * cannot blank the entire application.
 */
export class ErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error(
      `[ErrorBoundary] ${this.props.fallbackLabel ?? "Component"} crashed:`,
      error,
      info.componentStack,
    );
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError) {
      return (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            height: "100%",
            padding: 16,
            color: "#c9d1d9",
            background: "#161b22",
            fontSize: 12,
            gap: 8,
          }}
        >
          <span style={{ fontSize: 24 }}>⚠</span>
          <div style={{ fontWeight: 600 }}>
            {this.props.fallbackLabel ?? "Panel"} failed to load
          </div>
          <div style={{ color: "#8b949e", fontSize: 11, textAlign: "center" }}>
            {this.state.error?.message ?? "Unknown error"}
          </div>
          <button
            onClick={this.handleRetry}
            style={{
              marginTop: 4,
              background: "#21262d",
              border: "1px solid #30363d",
              color: "#c9d1d9",
              padding: "4px 12px",
              borderRadius: 4,
              cursor: "pointer",
              fontSize: 11,
            }}
          >
            Retry
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
