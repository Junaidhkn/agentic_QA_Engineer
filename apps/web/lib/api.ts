import createClient from "openapi-fetch";
import type { paths } from "@re/api-client";

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export interface ErrorEnvelope {
  error: { code: string; message: string; details?: Record<string, unknown>; request_id?: string };
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly details: Record<string, unknown> = {},
    public readonly requestId?: string,
  ) {
    super(message);
  }
}

type TokenGetter = () => Promise<string | null>;
let tokenGetter: TokenGetter = async () => null;
let orgGetter: () => string | null = () => null;

export function setOrgGetter(getter: () => string | null) {
  orgGetter = getter;
}

export function setTokenGetter(getter: TokenGetter) {
  tokenGetter = getter;
}

export function currentToken(): Promise<string | null> {
  return tokenGetter();
}

export function toApiError(status: number, body: unknown): ApiError {
  const env = body as Partial<ErrorEnvelope> | undefined;
  if (env?.error?.code) {
    return new ApiError(status, env.error.code, env.error.message, env.error.details ?? {}, env.error.request_id);
  }
  return new ApiError(status, "UNKNOWN", `request failed with status ${status}`);
}

/** Typed client generated from the control-plane OpenAPI document. */
export const client = createClient<paths>({ baseUrl: API_BASE_URL });
client.use({
  async onRequest({ request }) {
    const token = await tokenGetter();
    if (token) request.headers.set("Authorization", `Bearer ${token}`);
    const org = orgGetter();
    if (org && !request.headers.has("X-Org-Id")) request.headers.set("X-Org-Id", org);
    return request;
  },
});

/** Untyped escape hatch for endpoints whose response shapes are loosely typed (JSON blobs). */
export async function apiFetch<T>(path: string, init: RequestInit & { idempotencyKey?: string } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = await tokenGetter();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const org = orgGetter();
  if (org && !headers.has("X-Org-Id")) headers.set("X-Org-Id", org);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (init.idempotencyKey) headers.set("Idempotency-Key", init.idempotencyKey);
  const resp = await fetch(`${API_BASE_URL}${path}`, { ...init, headers });
  if (resp.status === 204) return undefined as T;
  const body = await resp.json().catch(() => undefined);
  if (!resp.ok) throw toApiError(resp.status, body);
  return body as T;
}

export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
