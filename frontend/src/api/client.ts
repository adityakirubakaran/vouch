import type {
  ActivityEvent,
  AppConfig,
  ApplicationDetail,
  ApplicationSummary,
  CallChannel,
  RingingCall,
  SampleInfo,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) {
    let message = res.statusText;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, message);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export const api = {
  config: () => request<AppConfig>("/api/config"),
  policy: () => request<Record<string, unknown>>("/api/policy"),
  samples: () => request<SampleInfo[]>("/api/samples"),
  applications: () => request<ApplicationSummary[]>("/api/applications"),
  application: (id: string) => request<ApplicationDetail>(`/api/applications/${id}`),
  events: (id: string) => request<ActivityEvent[]>(`/api/applications/${id}/events`),
  upload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{ id: string }>("/api/applications", { method: "POST", body: form });
  },
  fromSample: (sampleId: string) => post<{ id: string }>("/api/applications/from-sample", { sample_id: sampleId }),
  approve: (id: string, body: { limit?: number; terms?: string; note?: string }) =>
    post<ApplicationDetail>(`/api/applications/${id}/approve`, body),
  decline: (id: string, reason?: string) => post<ApplicationDetail>(`/api/applications/${id}/decline`, { reason }),
  requestInfo: (id: string, message?: string) =>
    post<ApplicationDetail>(`/api/applications/${id}/request-info`, { message }),
  retrySync: (id: string) => post<ApplicationDetail>(`/api/applications/${id}/sync`),
  setCallChannel: (id: string, channel: CallChannel) =>
    post<{ ok: boolean }>(`/api/applications/${id}/call-channel`, { channel }),
  simulateBankReply: (id: string) => post<{ email_id: string }>(`/api/applications/${id}/simulate-bank-reply`),
  callNow: (refId: string, channel?: CallChannel) =>
    post<{ ok: boolean }>(`/api/references/${refId}/call-now`, channel ? { channel } : {}),
  ringing: () => request<RingingCall[]>("/api/calls/ringing"),
};
