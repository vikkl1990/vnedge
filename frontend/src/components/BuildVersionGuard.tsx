import { useEffect } from "react";
import { useMeta } from "../queries";

const CLIENT_BUILD_SHA = String(import.meta.env.VITE_VNEDGE_BUILD_SHA ?? "dev").toLowerCase();
const RELOAD_KEY = "vnedge:last-build-reload";

/**
 * Replace a cockpit tab that survived an image deployment.
 *
 * The backend and static bundle are built from the same Docker build arg. A
 * mismatch therefore means the browser is still executing an older hashed JS
 * asset while polling a newer API. Reload at most once per server revision so
 * a partial/failed deployment cannot create a reload loop.
 */
export function BuildVersionGuard() {
  const meta = useMeta();

  useEffect(() => {
    const serverBuild = String(meta.data?.build_sha ?? "").toLowerCase();
    if (!serverBuild || serverBuild === "dev" || CLIENT_BUILD_SHA === "dev") return;
    if (serverBuild === CLIENT_BUILD_SHA) {
      window.sessionStorage.removeItem(RELOAD_KEY);
      return;
    }
    if (window.sessionStorage.getItem(RELOAD_KEY) === serverBuild) return;
    window.sessionStorage.setItem(RELOAD_KEY, serverBuild);
    window.location.reload();
  }, [meta.data?.build_sha]);

  return null;
}
