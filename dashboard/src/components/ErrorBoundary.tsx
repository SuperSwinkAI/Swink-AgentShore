import { Component, type ErrorInfo, type ReactNode } from "react";

import { dashboardLogger } from "../logger";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
  componentStack: string | null;
}

/**
 * Top-level error boundary for the dashboard React tree.
 *
 * Without a boundary, a single render-time throw (e.g. a component hitting an
 * unexpected field in a freshly-populated state_update after a page reload)
 * unmounts the entire root and leaves a blank white screen with no on-screen
 * trace. That presents to the user as "the dashboard crashed on reload".
 *
 * This boundary instead:
 *   - keeps the app mounted and renders a readable fallback,
 *   - shows the error message + component stack inline so the underlying bug
 *     is diagnosable without devtools, and
 *   - routes the failure through dashboardLogger.error (which bypasses the
 *     DEV/?debug gate, like the ws/server error paths) for the console.
 */
export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null, componentStack: null };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ componentStack: info.componentStack ?? null });
    dashboardLogger.error("react", "render error caught by boundary", {
      message: error.message,
      stack: error.stack,
      componentStack: info.componentStack,
    });
  }

  private handleReload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    const { error, componentStack } = this.state;
    if (error === null) {
      return this.props.children;
    }

    return (
      <div role="alert" className="dashboard-error-boundary">
        <h2>The dashboard hit a rendering error.</h2>
        <p>
          The session is unaffected — this is the browser view only. Reload to
          recover; if it recurs, the detail below pinpoints the cause.
        </p>
        <button type="button" onClick={this.handleReload}>
          Reload dashboard
        </button>
        <pre className="dashboard-error-boundary__detail">
          {error.message}
          {error.stack ? `\n\n${error.stack}` : ""}
          {componentStack ? `\n\nComponent stack:${componentStack}` : ""}
        </pre>
      </div>
    );
  }
}
