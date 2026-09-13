// Transport health is display-only. Never reinterpret it as trading permission.
export const LIVE_STALE_MS = 20_000;

export function snapshotTime(value: unknown): number | null {
  if (!value || typeof value !== "object" || !("ts" in value)) return null;
  const ts = (value as { ts?: unknown }).ts;
  if (typeof ts !== "string") return null;
  const at = Date.parse(ts);
  return Number.isFinite(at) ? at : null;
}

export function acceptSnapshot(incoming: unknown, current: unknown, now: number): boolean {
  const at = snapshotTime(incoming);
  const previous = snapshotTime(current);
  return at !== null && at <= now + 5_000 && (previous === null || at >= previous);
}

export function updatesFresh(snapshot: unknown, receivedAt: number, now: number): boolean {
  const at = snapshotTime(snapshot);
  return at !== null && at <= now + 5_000 && now - at <= LIVE_STALE_MS
    && receivedAt > 0 && now - receivedAt <= LIVE_STALE_MS;
}
