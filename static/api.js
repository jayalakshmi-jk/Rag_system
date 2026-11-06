import { getClient } from "./auth.js";

export class ApiError extends Error {
  constructor(status, code, message, requestId) {
    super(message);
    this.status = status; this.code = code; this.requestId = requestId;
  }
}

async function accessToken() {
  const client = await getClient();
  const { data } = await client.auth.getSession();
  return data.session?.access_token ?? null;
}

export async function api(path, { method = "GET", body, signal } = {}) {
  const attempt = async (token) => {
    const headers = { Accept: "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    return fetch(`/api${path}`, { method, headers, signal, body: body === undefined ? undefined : JSON.stringify(body) });
  };
  let response = await attempt(await accessToken());
  if (response.status === 401) {
    // Token may have just expired: refresh once, then retry.
    const client = await getClient();
    const { data } = await client.auth.refreshSession();
    if (data.session) response = await attempt(data.session.access_token);
  }
  if (response.status === 204) return null;
  let payload = null;
  try { payload = await response.json(); } catch { /* non-JSON error */ }
  if (!response.ok) {
    const err = payload?.error ?? {};
    throw new ApiError(response.status, err.code ?? "ERROR", err.message ?? "Request failed.", err.request_id);
  }
  return payload;
}
