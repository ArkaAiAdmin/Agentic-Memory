import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@ami/shared": path.resolve(__dirname, "../../packages/shared/src"),
      "@ami/memory-bridge": path.resolve(
        __dirname,
        "../../packages/memory-bridge/src",
      ),
      "@ami/llm": path.resolve(__dirname, "../../packages/llm/src"),
      "@ami/core": path.resolve(__dirname, "../../packages/core/src"),
    },
  },
  server: {
    port: 1420,
    strictPort: true,
  },
  build: {
    target: "esnext",
    minify: false,
  },
  clearScreen: false,
});
