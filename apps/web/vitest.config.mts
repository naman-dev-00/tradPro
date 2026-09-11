import { defineConfig } from "vitest/config";
import path from "path";

export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
    fileParallelism: false,
    maxConcurrency: 1,
    include: ["tests/**/*.test.ts"],
    exclude: ["e2e/**", "node_modules/**"],
  },
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname || ".", "./src"),
    },
  },
});
