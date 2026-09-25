import { Component, type ErrorInfo, type ReactNode } from "react";

/** Keeps one broken widget (or page) from blanking the whole app. */
export class ErrorBoundary extends Component<{ children: ReactNode; label?: string; resetKey?: unknown }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("UI error", error, info.componentStack);
  }

  componentDidUpdate(prev: { resetKey?: unknown }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }

  render() {
    if (this.state.error) {
      return (
        <div className="alert alert-danger" role="alert">
          <strong>{this.props.label ?? "This view failed to render"}:</strong> <span>{this.state.error.message}</span>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => this.setState({ error: null })}>Retry</button>
        </div>
      );
    }
    return this.props.children;
  }
}
