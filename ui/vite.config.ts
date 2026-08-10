import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { mockApi } from "./mock/plugin";

export default defineConfig({
  plugins: [react(), mockApi()],
  server: { port: 5173 },
  preview: { port: 4173 },
});
