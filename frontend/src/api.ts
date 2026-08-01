// Thin fetch client over the existing token-gated dashboard endpoints.
// The token is read from the ?token= query param (same convention as the
// classic dashboard) and sent as a Bearer header — never placed in logs.

export function getToken(): string {
  return new URLSearchParams(window.location.search).get("token") ?? "";
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (res.status === 401) throw new ApiError(401, "unauthorized — check the token");
  if (!res.ok) throw new ApiError(res.status, `request failed (${res.status})`);
  return (await res.json()) as T;
}

// --- Response shapes (only the fields the panels read) ---------------------
export interface WhoAmI {
  name: string | null;
  role: string | null;
  permissions: string[];
}

export interface Snapshot {
  mode?: string;
  equity?: number;
  [k: string]: unknown;
}

export interface JournalRow {
  lane?: string;
  symbol?: string;
  side?: string;
  net_pnl_usd?: number;
  exit_reason?: string;
  [k: string]: unknown;
}
