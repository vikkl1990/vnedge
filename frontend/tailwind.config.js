/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0B0E11", panel: "#161B22", panel2: "#1C2128", inset: "#111318",
        line: "#30363D", line2: "#3D444D",
        txt: "#E6EDF3", dim: "#8B949E", faint: "#6E7681",
        brand: "#58A6FF", info: "#58A6FF", warn: "#D29922",
        long: "#3FB950", short: "#F85149",
      },
      fontFamily: {
        mono: ['ui-monospace', 'SF Mono', 'JetBrains Mono', 'Menlo', 'monospace'],
        sans: ['SF Pro Text', '-apple-system', 'Segoe UI', 'system-ui', 'sans-serif'],
      },
      borderRadius: { xl: "8px", md: "6px" },
    },
  },
  plugins: [],
};
