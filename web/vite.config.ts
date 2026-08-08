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

function getApiOrigin(value?: string): string {
  const raw = value || "http://127.0.0.1:8000";
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("API_ORIGIN must be an absolute HTTP(S) origin");
  }
  const isHttp = url.protocol === "http:" || url.protocol === "https:";
  const isOriginOnly =
    !url.username && !url.password && url.pathname === "/" && !url.search && !url.hash;
  if (!isHttp || !isOriginOnly) {
    throw new Error("API_ORIGIN must be an HTTP(S) origin without credentials or a path");
  }
  return url.origin;
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");

  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": getApiOrigin(env.API_ORIGIN),
      },
    },
    preview: {
      allowedHosts: getPreviewAllowedHosts(env.PUBLIC_ORIGIN),
    },
  };
});
