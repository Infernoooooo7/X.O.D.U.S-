import type { Metadata, Viewport } from "next";
import "./globals.css";
import { AppShell } from "@/components/shell/app-shell";
import { THEME_SCRIPT } from "@/lib/theme-script";

// Fonts are the ones Windows already has (Segoe UI Variable, Cascadia Mono): nothing is downloaded at build or run time.

export const metadata: Metadata = {
  title: "XODUS",
  description: "Local-first personal AI workspace",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: "#05090d",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Runs before paint: applies the saved theme (dark unless "light" or "follow Windows" was chosen). */}
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
