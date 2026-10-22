import { svelte } from "@sveltejs/vite-plugin-svelte";
import { resolve } from "node:path";
import { defineConfig } from "vitest/config";

const root = resolve(import.meta.dirname, "../..");

export default defineConfig({
  root,
  plugins: [
    svelte({ configFile: false }),
    {
      name: "resolve-cloud-portal-workspace-dependencies",
      enforce: "pre",
      resolveId(source) {
        if (source === "svelte" || source.startsWith("svelte/")) {
          return this.resolve(
            source,
            resolve(root, "apps/cloud-portal/src/routes/+page.svelte"),
            { skipSelf: true },
          );
        }
        return null;
      },
    },
  ],
  resolve: {
    conditions: ["browser", "development"],
    alias: {
      "@harnessa/ui-svelte": resolve(root, "packages/ui-svelte/src/lib/index.ts"),
    },
  },
  test: {
    environment: "jsdom",
    include: ["tests/web/svelte.test.ts"],
    deps: { optimizer: { client: { enabled: false }, ssr: { enabled: false } } },
    coverage: {
      provider: "v8",
      include: [
        "apps/cloud-portal/src/**/*.{ts,svelte}",
        "packages/ui-svelte/src/lib/**/*.{ts,svelte}",
      ],
      exclude: ["**/*.d.ts", "**/*.css", "**/*.test.{ts,tsx}"],
      reporter: ["json", "text-summary"],
      reportsDirectory: "coverage/svelte",
    },
  },
});
