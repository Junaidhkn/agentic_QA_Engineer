import { API_BASE_URL, toApiError } from "@/lib/api";

/** Unauthenticated GET for public endpoints (legal documents, plans). */
export async function publicFetch<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`);
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) throw toApiError(res.status, body);
  return body as T;
}
