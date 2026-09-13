import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../api";

interface ServiceStatus {
  status: "fresh" | "stale" | "unknown";
  age_seconds: number | null;
  services: { name: string; running: boolean; health: string; action: string; restart_attempts_1h: number }[];
}

export function ServiceHealth() {
  const { data, isError } = useQuery({ queryKey: ["service-health"],
    queryFn: () => apiGet<ServiceStatus>("/api/services"), refetchInterval: 15_000 });
  const fresh = data?.status === "fresh" && !isError;
  return <section className="rounded-lg border border-line bg-inset p-3" aria-label="Service availability">
    <header className="mb-3 text-xs font-mono">SERVICE AVAILABILITY · {fresh ? "host checks active" : "status stale / unavailable — retrying"} · not trading readiness</header>
    <div className="flex flex-wrap gap-2">{data?.services.map(service => {
      const ok = fresh && service.running && service.health === "healthy";
      const label = !fresh ? "UNKNOWN" : !service.running ? "STOPPED" : service.health === "unprobed" ? "RUNNING · UNPROBED" : service.health.toUpperCase();
      return <span key={service.name} className={`rounded border border-line px-2 py-1 text-[10px] font-mono ${ok ? "text-long" : "text-warn"}`} title={`${service.action} · ${service.restart_attempts_1h} recovery attempts/hour`}>
        {service.name} · {label}
      </span>;
    })}</div>
  </section>;
}
