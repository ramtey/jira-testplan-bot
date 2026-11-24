import { describe, it, expect, vi, beforeEach } from "vitest";
import { buildApp } from "../src/app";
import * as verify from "../src/shared/auth/verify";

const member = { uid: "u-1", clientId: "c-1", scopes: ["files.read"] };

describe("POST /mcp", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("requires a bearer token", async () => {
    const res = await buildApp().inject({ method: "POST", url: "/mcp", headers: { host: "mcp.example.test" },
      payload: { jsonrpc: "2.0", id: 1, method: "tools/list" } });
    expect(res.statusCode).toBe(401);
    expect(res.json().error_description).toBe("A bearer token is required.");
  });

  it("refuses a batch request with -32600", async () => {
    vi.spyOn(verify, "verifyBearer").mockResolvedValue(member);
    const res = await buildApp().inject({ method: "POST", url: "/mcp",
      headers: { host: "mcp.example.test", authorization: "Bearer t" },
      payload: [{ jsonrpc: "2.0", id: 1, method: "tools/list" }, { jsonrpc: "2.0", id: 2, method: "tools/list" }] });
    expect(res.statusCode).toBe(400);
    expect(res.json().error.code).toBe(-32600);
  });

  it("answers a wrong Host with the same 421 as before", async () => {
    vi.spyOn(verify, "verifyBearer").mockResolvedValue(member);
    const res = await buildApp().inject({ method: "POST", url: "/mcp",
      headers: { host: "evil.example", authorization: "Bearer t" },
      payload: { jsonrpc: "2.0", id: 1, method: "tools/list" } });
    expect(res.statusCode).toBe(421);
    expect(res.json()).toEqual({ error: "misdirected_request" });
  });

  it("answers 503 with Retry-After when the key set cannot be fetched", async () => {
    vi.spyOn(verify, "verifyBearer").mockRejectedValue(new verify.KeySetUnavailableError("down"));
    const res = await buildApp().inject({ method: "POST", url: "/mcp",
      headers: { host: "mcp.example.test", authorization: "Bearer t" },
      payload: { jsonrpc: "2.0", id: 1, method: "tools/list" } });
    expect(res.statusCode).toBe(503);
    expect(res.headers["retry-after"]).toBe("5");
    expect(res.headers["www-authenticate"]).toBeUndefined();
  });
});
