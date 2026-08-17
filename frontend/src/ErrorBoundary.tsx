import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("VNEDGE cockpit render fault", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <main className="min-h-screen grid place-items-center bg-bg px-5 text-txt">
        <section role="alert" className="w-full max-w-xl rounded-xl border border-short/50 bg-panel p-7 shadow-2xl">
          <div className="font-mono text-[11px] uppercase tracking-wider text-short">Cockpit render fault</div>
          <h1 className="mt-2 text-xl font-semibold">The dashboard isolated an unexpected UI failure.</h1>
          <p className="mt-3 text-sm leading-relaxed text-dim">
            Runtime and trading policy are unchanged. Treat all unseen state as unknown until the cockpit reloads.
          </p>
          <pre className="mt-4 max-h-32 overflow-auto rounded-md border border-line bg-inset p-3 font-mono text-[11px] text-short">
            {this.state.error.message || "unknown render error"}
          </pre>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="mt-5 rounded-md border border-brand/60 bg-brand/10 px-4 py-2 font-mono text-sm text-brand"
          >
            Reload cockpit
          </button>
        </section>
      </main>
    );
  }
}
