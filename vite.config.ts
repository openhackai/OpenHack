import { defineConfig } from "vite";
export default defineConfig({
  root: "web",
  build: {
    outDir: "../dist/web",
    emptyOutDir: true,
    rolldownOptions: {
      onwarn(warning, warn) {
        // This is entirely client-rendered; dependency RSC directives are unnecessary.
        if (
          warning.code === "MODULE_LEVEL_DIRECTIVE" &&
          warning.message.includes("use client")
        )
          return;
        warn(warning);
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${process.env.OPENHACK_DEV_API_PORT ?? 6969}`,
        changeOrigin: true,
      },
    },
  },
});
