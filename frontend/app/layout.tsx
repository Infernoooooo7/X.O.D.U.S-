import type { Metadata, Viewport } from "next";
import "./globals.css";
import { AppShell } from "@/components/shell/app-shell";

// Fonts are the ones Windows already has (Segoe UI Variable, Cascadia Mono): nothing is downloaded at build or run time.

export const metadata: Metadata = {
  title: "XODUS",
  description: "Local-first personal AI workspace",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#e6ded4" },
    { media: "(prefers-color-scheme: dark)", color: "#1c1a17" },
  ],
};

// Runs before paint so a stored theme choice never flashes the wrong theme.
const themeScript = `try{var t=localStorage.getItem("xodus-theme");if(t==="dark"||(t!=="light"&&matchMedia("(prefers-color-scheme: dark)").matches))document.documentElement.classList.add("dark")}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
