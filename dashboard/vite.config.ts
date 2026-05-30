import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { basename } from "path";
import { readFileSync } from "fs";

function emitPngAssets(): Plugin {
  return {
    name: "emit-png-assets",
    enforce: "pre",
    load(id) {
      if (!id.endsWith(".png")) return null;
      const ref = this.emitFile({
        type: "asset",
        name: basename(id),
        source: readFileSync(id),
      });
      return `export default import.meta.ROLLUP_FILE_URL_${ref};`;
    },
  };
}

export default defineConfig(({ mode }) => {
  if (mode === "lib") {
    return {
      plugins: [emitPngAssets(), react()],
      build: {
        lib: {
          entry: "src/index.ts",
          formats: ["es"],
          fileName: "index",
        },
        outDir: "dist",
        emptyOutDir: true,
        rollupOptions: {
          external: ["react", "react-dom", "react/jsx-runtime"],
        },
      },
    };
  }

  const wsTarget =
    process.env.AGENTSHORE_DASHBOARD_WS_TARGET ?? "ws://localhost:9400";

  return {
    plugins: [react()],
    build: {
      outDir: "../src/agentshore/dashboard/static",
      emptyOutDir: true,
    },
    server: {
      proxy: {
        "/ws": {
          target: wsTarget,
          ws: true,
        },
      },
    },
    test: {
      include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
      setupFiles: ["./tests/setup.ts"],
    },
  };
});
