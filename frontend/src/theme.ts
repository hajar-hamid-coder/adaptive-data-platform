import { useCallback, useEffect, useState } from "react";

const KEY = "adp-theme"; // the only thing stored locally: "light" | "dark"
export type Theme = "light" | "dark";

const stored = (): Theme | null => {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
};

const systemDark = (): boolean =>
  typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: dark)").matches;

/** Explicit choice (data-theme on <html>) or, by default, the system preference. */
export function useTheme(): { theme: Theme; toggle: () => void } {
  const [choice, setChoice] = useState<Theme | null>(stored);
  useEffect(() => {
    const root = document.documentElement;
    if (choice) root.setAttribute("data-theme", choice);
    else root.removeAttribute("data-theme");
  }, [choice]);
  const theme: Theme = choice ?? (systemDark() ? "dark" : "light");
  const toggle = useCallback(() => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setChoice(next);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      /* storage unavailable: theme still applies for this session */
    }
  }, [theme]);
  return { theme, toggle };
}
