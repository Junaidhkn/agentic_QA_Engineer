import { describe, expect, it } from "vitest";

import { ApiError, toApiError } from "@/lib/api";

describe("toApiError", () => {
  it("maps the error envelope", () => {
    const err = toApiError(409, {
      error: { code: "TARGET_NOT_VERIFIED", message: "not verified", details: { a: 1 }, request_id: "req_1" },
    });
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("TARGET_NOT_VERIFIED");
    expect(err.status).toBe(409);
    expect(err.requestId).toBe("req_1");
  });

  it("falls back for non-envelope bodies", () => {
    expect(toApiError(502, "bad gateway").code).toBe("UNKNOWN");
  });
});
