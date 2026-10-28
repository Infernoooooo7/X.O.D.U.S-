// Kept apart from lib/theme.ts (which uses React hooks) because app/layout.tsx, a server component, inlines it.
// Runs before the first paint: dark unless the saved choice is "light", or "system" while Windows uses a light theme.

export const THEME_KEY = "xodus-theme";

export const THEME_SCRIPT = `try{var t=localStorage.getItem("${THEME_KEY}");var l=t==="light"||(t==="system"&&matchMedia("(prefers-color-scheme: light)").matches);if(!l)document.documentElement.classList.add("dark")}catch(e){document.documentElement.classList.add("dark")}`;
