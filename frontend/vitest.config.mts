import { defineConfig } from "vitest/config";

// Unit and component tests (jsdom). JSX is compiled by Vite's built-in transform (tsconfig: "jsx": "react-jsx").
export default defineConfig({
  resolve: { alias: { "@": import.meta.dirname } },
  test: {
    environment: "jsdom",
    include: ["**/__tests__/**/*.test.{ts,tsx}"],
    exclude: ["node_modules/**", ".next/**", "out/**"],
  },
});
