# VN Edge UI (v2) — React foundation

A React / Vite / TypeScript / Tailwind rebuild of the dashboard, using
**TanStack Query** over the existing token-gated endpoints (`/state`,
`/journal`, `/whoami`). Zustand holds UI chrome only; all server state lives in
TanStack Query. Ships a small **Terminal\*** component library (`TerminalPanel`,
`DenseTable`, `TerminalBadge`, `TerminalTabs`) and a **Ctrl/Cmd-K** command
palette.

## Status
**Foundation + core panels (Desk book, Journal), not full parity.** The classic
dashboard remains the primary cockpit at `/`; this ships alongside it at `/app`.
Remaining classic panels port onto these same primitives incrementally.

## Build & serve
```bash
npm --prefix frontend install
npm --prefix frontend run build      # -> frontend/dist
```
`create_app` mounts `frontend/dist` at **`/app`** *only when the build exists*
— so a production image without the build simply has no `/app` route (never a
500), and `/` is unaffected. Open `/app/`; the sign-in gate exchanges the root
token for a short-lived HttpOnly cookie without putting credentials in the URL
or browser storage.

## Production
The Docker build includes the compiled React distribution. Build and verify the
assets before deployment; production access requires HTTPS when the default
`DASHBOARD_COOKIE_SECURE=true` is retained.
