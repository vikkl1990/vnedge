import { type FormEvent, type ReactNode, useEffect, useState } from "react";
import {
  establishBrowserSession,
  hasBrowserSession,
  keepBrowserSessionAlive,
} from "./api";

export function AuthGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<"checking" | "required" | "ready">("checking");
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [expiresAt, setExpiresAt] = useState<string | null>(null);

  useEffect(() => {
    void hasBrowserSession()
      .then((session) => {
        setExpiresAt(session?.expires_at ?? null);
        setStatus(session ? "ready" : "required");
      })
      .catch(() => {
        setError("Dashboard unavailable — check the secure connection and try again.");
        setStatus("required");
      });
    const expired = () => {
      setExpiresAt(null);
      setError("Session expired or was invalidated. Authenticate again.");
      setStatus("required");
    };
    window.addEventListener("vnedge-auth-expired", expired);
    return () => window.removeEventListener("vnedge-auth-expired", expired);
  }, []);

  useEffect(() => {
    if (status !== "ready") return;
    return keepBrowserSessionAlive(expiresAt, (session) => {
      setExpiresAt(session.expires_at);
    });
  }, [expiresAt, status]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      const session = await establishBrowserSession(token);
      setToken("");
      setExpiresAt(session.expires_at);
      setStatus("ready");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "authentication failed");
    } finally {
      setSubmitting(false);
    }
  }

  if (status === "ready") return children;
  if (status === "checking") return (
    <main className="min-h-screen grid place-items-center bg-bg text-dim font-mono">
      Checking session…
    </main>
  );
  return (
    <main className="min-h-screen grid place-items-center bg-bg px-5 text-txt">
      <form onSubmit={(event) => void submit(event)} className="w-full max-w-md rounded-lg border border-line bg-panel p-7 shadow-2xl">
        <div className="mb-6 flex items-center gap-3">
          <div className="grid h-11 w-11 place-items-center rounded-md border border-accent/60 font-mono text-accent">VN</div>
          <div>
            <h1 className="text-lg font-semibold">VNEDGE</h1>
            <p className="font-mono text-[11px] text-faint">secure operator session</p>
          </div>
        </div>
        <label htmlFor="dashboard-token" className="block font-mono text-[11px] uppercase tracking-wider text-dim">
          Dashboard token
        </label>
        <input
          id="dashboard-token"
          type="password"
          autoComplete="current-password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          autoFocus
          className="mt-2 w-full rounded-md border border-line bg-bg px-3 py-2.5 font-mono text-sm outline-none focus:border-accent"
        />
        {error && <p role="alert" className="mt-3 text-sm text-short">{error}</p>}
        <button disabled={submitting || !token.trim()} className="mt-5 w-full rounded-md border border-accent/60 bg-accent/10 px-4 py-2.5 font-mono text-sm text-accent disabled:opacity-40">
          {submitting ? "Authenticating…" : "Start secure session"}
        </button>
        <p className="mt-4 text-[11px] leading-relaxed text-faint">
          Over HTTPS, the credential is sent once in an authorization header. It is never placed in the URL or browser storage.
        </p>
      </form>
    </main>
  );
}
