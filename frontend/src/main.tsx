import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { establishBrowserSession, keepBrowserSessionAlive } from "./api";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true } },
});

async function bootstrap() {
  await establishBrowserSession();
  keepBrowserSessionAlive();
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </StrictMode>,
  );
}

void bootstrap().catch((error: unknown) => {
  const root = document.getElementById("root");
  if (root) root.textContent = error instanceof Error ? error.message : "session bootstrap failed";
});
