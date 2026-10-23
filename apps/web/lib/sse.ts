import { API_BASE_URL } from "@/lib/api";

export interface SseMessage { id: number; event: string; data: unknown }

/**
 * Server-Sent Events over fetch (EventSource cannot send the Authorization header).
 * Reconnects with Last-Event-ID so no events are lost (ADR-0021).
 */
export function subscribeRunEvents(
  runId: string,
  getToken: () => Promise<string | null>,
  onMessage: (m: SseMessage) => void,
  onEnd: () => void,
): () => void {
  let lastId = 0;
  let stopped = false;
  let controller: AbortController | null = null;

  async function connect(attempt: number): Promise<void> {
    if (stopped) return;
    controller = new AbortController();
    try {
      const token = await getToken();
      const resp = await fetch(`${API_BASE_URL}/v1/runs/${runId}/events`, {
        headers: { Accept: "text/event-stream", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(lastId ? { "Last-Event-ID": String(lastId) } : {}) },
        signal: controller.signal,
      });
      if (!resp.ok || !resp.body) throw new Error(`stream failed: ${resp.status}`);
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const raw = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          const msg = parseFrame(raw);
          if (!msg) continue;
          lastId = Math.max(lastId, msg.id);
          if (msg.event === "end") { stopped = true; onEnd(); return; }
          onMessage(msg);
        }
      }
    } catch {
      if (stopped) return;
    }
    if (!stopped) setTimeout(() => connect(attempt + 1), Math.min(10_000, 500 * 2 ** attempt));
  }

  void connect(0);
  return () => { stopped = true; controller?.abort(); };
}

export function parseFrame(raw: string): SseMessage | null {
  let id = 0;
  let event = "message";
  const data: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("id: ")) id = Number(line.slice(4));
    else if (line.startsWith("event: ")) event = line.slice(7);
    else if (line.startsWith("data: ")) data.push(line.slice(6));
  }
  if (!data.length) return null;
  try {
    return { id, event, data: JSON.parse(data.join("\n")) };
  } catch {
    return null;
  }
}
