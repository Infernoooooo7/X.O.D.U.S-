"use client";

import { useEffect, useState } from "react";
import { THEME_KEY as KEY } from "./theme-script";

// Dark is the default. The choice is saved in this browser only (localStorage "xodus-theme"); the inline script from
// lib/theme-script.ts applies it before the first paint, so the page never flashes the other theme.

export type ThemeChoice = "dark" | "light" | "system";
const EVENT = "xodus:theme";

export function readTheme(): ThemeChoice {
  try {
    const t = localStorage.getItem(KEY);
    return t === "light" || t === "system" ? t : "dark";
  } catch {
    return "dark";
  }
}

export function isDark(choice: ThemeChoice): boolean {
  return choice === "dark" || (choice === "system" && !matchMedia("(prefers-color-scheme: light)").matches);
}

export function setTheme(choice: ThemeChoice): void {
  try {
    if (choice === "dark") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, choice);
  } catch { /* storage unavailable: the choice lasts for this page only */ }
  document.documentElement.classList.toggle("dark", isDark(choice));
  window.dispatchEvent(new CustomEvent(EVENT, { detail: choice }));
}

/** The current choice and whether the page is dark now; follows changes made anywhere on the page. */
export function useTheme(): { choice: ThemeChoice; dark: boolean; set: (c: ThemeChoice) => void } {
  const [choice, setChoice] = useState<ThemeChoice>("dark");
  const [dark, setDark] = useState(true);
  useEffect(() => {
    const sync = () => {
      setChoice(readTheme());
      setDark(document.documentElement.classList.contains("dark"));
    };
    sync();
    window.addEventListener(EVENT, sync);
    const media = matchMedia("(prefers-color-scheme: light)");
    const onMedia = () => { if (readTheme() === "system") setTheme("system"); };
    media.addEventListener("change", onMedia);
    return () => { window.removeEventListener(EVENT, sync); media.removeEventListener("change", onMedia); };
  }, []);
  return { choice, dark, set: setTheme };
}
