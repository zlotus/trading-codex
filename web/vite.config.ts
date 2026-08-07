import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

function getPreviewAllowedHosts(publicOrigin?: string): string[] {
  if (!publicOrigin) {
    return [];
  }

  let url: URL;
  try {
    url = new URL(publicOrigin);
  } catch {
    throw new Error("PUBLIC_ORIGIN must be an absolute HTTP(S) origin");
  }

  const isHttp = url.protocol === "http:" || url.protocol === "https:";
  const isOriginOnly =
    !url.username && !url.password && url.pathname === "/" && !url.search && !url.hash;
  if (!isHttp || !isOriginOnly) {
    throw new Error(
      "PUBLIC_ORIGIN must use HTTP(S) and must not contain credentials, a path, query, or fragment",
    );
  }

  return [url.hostname];
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");

  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": "http://127.0.0.1:8000",
      },
    },
    preview: {
      allowedHosts: getPreviewAllowedHosts(env.PUBLIC_ORIGIN),
    },
  };
});
