// Zustand for UI chrome only (per the roadmap) — server state lives in
// TanStack Query, never here. This holds just the command-palette open flag.
import { create } from "zustand";

interface UiState {
  paletteOpen: boolean;
  selectedLaneId: string;
  setPalette: (open: boolean) => void;
  setSelectedLane: (laneId: string) => void;
  togglePalette: () => void;
}

export const useUi = create<UiState>((set) => ({
  paletteOpen: false,
  selectedLaneId: "",
  setPalette: (open) => set({ paletteOpen: open }),
  setSelectedLane: (selectedLaneId) => set({ selectedLaneId }),
  togglePalette: () => set((s) => ({ paletteOpen: !s.paletteOpen })),
}));
