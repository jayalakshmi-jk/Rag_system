import { getClient } from "./auth.js";

export class ApiError extends Error {
  constructor(status, code, message, requestId, extra = {}) {
    super(message);
    this.status = status; this.code = code; this.requestId = requestId; this.extra = extra;
  }
}

async function accessToken() {
  const client = await getClient();
  const { data } = await client.auth.getSession();
  return data.session?.access_token ?? null;
}

// Sends an authenticated request; on 401 refreshes the session once and retries.
async function authedFetch(path, makeInit) {
  const attempt = async (token) => {
    const init = makeInit();
    init.headers = { Accept: "application/json", ...(init.headers || {}) };
    if (token) init.headers.Authorization = `Bearer ${token}`;
    return fetch(`/api${path}`, init);
  };
  let response = await attempt(await accessToken());
  if (response.status === 401) {
    const client = await getClient();
    const { data } = await client.auth.refreshSession();
    if (data.session) response = await attempt(data.session.access_token);
  }
  return response;
}

async function toError(response) {
  let payload = null;
  try { payload = await response.json(); } catch { /* non-JSON error */ }
  const { code, message, request_id: requestId, ...extra } = payload?.error ?? {};
  return new ApiError(response.status, code ?? "ERROR", message ?? "Request failed.", requestId, extra);
}

export async function api(path, { method = "GET", body, signal } = {}) {
  const response = await authedFetch(path, () => ({
    method, signal,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }));
  if (response.status === 204) return null;
  if (!response.ok) throw await toError(response);
  return response.json();
}

export async function upload(path, formData, { signal } = {}) {
  const response = await authedFetch(path, () => ({ method: "POST", body: formData, signal }));
  if (!response.ok) throw await toError(response);
  return response.json();
}

// POSTs JSON and yields parsed Server-Sent Events: { event, data }.
export async function* stream(path, body, { signal } = {}) {
  const response = await authedFetch(path, () => ({
    method: "POST", signal, body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
  }));
  if (!response.ok) throw await toError(response);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const event = /^event: (.+)$/m.exec(block)?.[1];
      const data = /^data: (.+)$/m.exec(block)?.[1];
      if (event && data) yield { event, data: JSON.parse(data) };
    }
  }
}
