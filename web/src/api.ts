import type {
  LedgerDashboard,
  RecordFillInput,
  SignalDetail,
  SkipSignalInput,
  SystemStatus,
} from "./types";

async function readJson<T>(response: Response, message: string): Promise<T> {
  if (!response.ok) {
    let detail = message;
    try {
      const payload = await response.json() as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      // The status code still identifies the failed request when no JSON body exists.
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export async function getSystemStatus(signal?: AbortSignal): Promise<SystemStatus> {
  const response = await fetch("/api/v1/system/status", { signal });
  return readJson<SystemStatus>(response, `System status request failed: ${response.status}`);
}

export async function getLedgerDashboard(signal?: AbortSignal): Promise<LedgerDashboard> {
  const response = await fetch("/api/v1/ledger/dashboard", { signal });
  return readJson<LedgerDashboard>(response, `Ledger request failed: ${response.status}`);
}

export async function getSignalDetail(
  signalId: string,
  signal?: AbortSignal,
): Promise<SignalDetail> {
  const response = await fetch(`/api/v1/ledger/signals/${encodeURIComponent(signalId)}`, {
    signal,
  });
  return readJson<SignalDetail>(response, `Signal request failed: ${response.status}`);
}

export async function recordFill(input: RecordFillInput): Promise<SignalDetail> {
  const response = await fetch("/api/v1/ledger/fills", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return readJson<SignalDetail>(response, `Fill request failed: ${response.status}`);
}

export async function skipSignal(
  signalId: string,
  input: SkipSignalInput,
): Promise<SignalDetail> {
  const response = await fetch(
    `/api/v1/ledger/signals/${encodeURIComponent(signalId)}/skip`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
  );
  return readJson<SignalDetail>(response, `Skip request failed: ${response.status}`);
}
