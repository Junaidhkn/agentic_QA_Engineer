import { describe, expect, it } from "vitest";

import { parseFrame } from "@/lib/sse";

describe("parseFrame", () => {
  it("parses id, event and JSON data", () => {
    expect(parseFrame('id: 7\nevent: progress\ndata: {"rps":12}')).toEqual({ id: 7, event: "progress", data: { rps: 12 } });
  });
  it("ignores comments/heartbeats", () => {
    expect(parseFrame(": heartbeat")).toBeNull();
  });
});
