import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Two apps, one build: chat.html (patients, served at /) and admin.html (staff, served at /admin).
// FastAPI serves the built files from /ui (backend/app/main.py). public/widget.js -> /widget.js.
// Dev: `npm run dev`, then open http://localhost:5173/ui/chat.html or /ui/admin.html (API proxied to :8000).
export default defineConfig({
  base: "/ui/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  build: {
    rollupOptions: {
      input: { chat: path.resolve(__dirname, "chat.html"), admin: path.resolve(__dirname, "admin.html") },
      output: {
        manualChunks(id) {
          if (id.includes("@fullcalendar")) return "calendar";
          if (id.includes("node_modules/react") || id.includes("node_modules/scheduler")) return "react";
          return undefined;
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": process.env.API_URL ?? "http://localhost:8000",
      "/branding": process.env.API_URL ?? "http://localhost:8000",
    },
  },
});
