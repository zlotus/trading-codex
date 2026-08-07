import type { SystemStatus } from "./types";

export async function getSystemStatus(signal?: AbortSignal): Promise<SystemStatus> {
  const response = await fetch("/api/v1/system/status", { signal });
  if (!response.ok) {
    throw new Error(`System status request failed: ${response.status}`);
  }
  return response.json() as Promise<SystemStatus>;
}
