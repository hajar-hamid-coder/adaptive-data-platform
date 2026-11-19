import type { Config } from "tailwindcss";

const v = (name: string): string => `var(--adp-${name})`;

// Token -> class table: frontend/DESIGN.md section 2.5. Values stay CSS variables (theming in tokens.css).
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    screens: { sm: "640px", md: "900px", lg: "1200px" },
    extend: {
      colors: {
        bg: v("bg"),
        surface: v("surface"),
        "surface-2": v("surface-2"),
        border: v("border"),
        "border-strong": v("border-strong"),
        fg: v("text"),
        "fg-muted": v("text-muted"),
        accent: v("accent"),
        "on-accent": v("on-accent"),
        focus: v("focus"),
        ok: v("ok"),
        "ok-bg": v("ok-bg"),
        warn: v("warn"),
        "warn-bg": v("warn-bg"),
        error: v("error"),
        "error-bg": v("error-bg"),
        info: v("info"),
        "info-bg": v("info-bg"),
        quarantine: v("quarantine"),
        "quarantine-bg": v("quarantine-bg"),
        chart: { 1: v("chart-1"), 2: v("chart-2"), 3: v("chart-3"), 4: v("chart-4"), 5: v("chart-5") },
      },
      fontFamily: { sans: [v("font-sans")], mono: [v("font-mono")] },
      fontSize: {
        xs: v("text-xs"),
        sm: v("text-sm"),
        base: v("text-base"),
        lg: v("text-lg"),
        xl: v("text-xl"),
        "2xl": v("text-2xl"),
      },
      borderRadius: { sm: v("radius-sm"), md: v("radius-md"), lg: v("radius-lg"), full: v("radius-full") },
      boxShadow: { card: v("shadow") },
      transitionDuration: { fast: v("dur-fast"), base: v("dur-base"), slow: v("dur-slow") },
      transitionTimingFunction: { adp: v("ease") },
    },
  },
} satisfies Config;
