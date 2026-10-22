import { resolve } from "node:path";
import { defineConfig } from "vitest/config";

const root = resolve(import.meta.dirname);

export default defineConfig({
  root,
  resolve: {
    alias: { "@": resolve(root, "apps/web") },
  },
  test: {
    environment: "jsdom",
    include: ["packages/**/*.test.{ts,tsx}", "apps/web/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      include: [
        "apps/web/**/*.{ts,tsx}",
        "packages/ui-react/src/**/*.{ts,tsx}",
      ],
      exclude: [
        "**/*.d.ts",
        "**/*.css",
        "**/*.test.{ts,tsx}",
        "**/.next/**",
        "packages/contracts/src/**",
      ],
      reporter: ["json", "text-summary"],
      reportsDirectory: "coverage/javascript",
    },
  },
});
