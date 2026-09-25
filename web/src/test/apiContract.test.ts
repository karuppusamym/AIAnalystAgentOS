import { describe, expect, it } from "vitest";
import { apiPath } from "../api";
import spec from "../../openapi.json";

/**
 * The client is bound to the generated OpenAPI types. The `@ts-expect-error` lines are checked by
 * `npm run typecheck`: if any of them stops being an error, the contract binding has loosened.
 */
function typeLevelChecks() {
  // @ts-expect-error unknown route
  apiPath("get", "/api/workspaces/{workspace_id}/nope", { path: { workspace_id: "x" } });
  // @ts-expect-error wrong method for a route
  apiPath("delete", "/api/auth/me", {});
  // @ts-expect-error wrong path parameter name
  apiPath("get", "/api/workspaces/{workspace_id}/catalog", { path: { wrong: "x" } });
  // @ts-expect-error unknown query parameter
  apiPath("get", "/api/workspaces/{workspace_id}/catalog", { path: { workspace_id: "x" }, query: { bogus: 1 } });
  // @ts-expect-error request body field of the wrong type
  apiPath("post", "/api/workspaces/{workspace_id}/schedules", { path: { workspace_id: "x" }, body: { name: 1, kind: "k", cron: "c" } });
  // @ts-expect-error missing required path parameters
  apiPath("get", "/api/workspaces/{workspace_id}", {});
}

describe("generated API contract", () => {
  it("fills path parameters, encodes them and drops empty query values", () => {
    expect(apiPath("get", "/api/workspaces/{workspace_id}/catalog", {
      path: { workspace_id: "ws 1" }, query: { q: "orders", domain: undefined, role: null, include_deprecated: true },
    })).toBe("/workspaces/ws%201/catalog?q=orders&include_deprecated=true");
    expect(apiPath("get", "/api/auth/me", {})).toBe("/auth/me");
    expect(typeof typeLevelChecks).toBe("function");
  });

  it("uses only routes that exist in the committed OpenAPI schema", async () => {
    const source = await import("../api.ts?raw").then((m: { default: string }) => m.default);
    const used = new Set([...source.matchAll(/"(\/api\/[^"]+)"/g)].map((m) => m[1]));
    const known = new Set(Object.keys((spec as { paths: Record<string, unknown> }).paths));
    expect(used.size).toBeGreaterThan(50);
    expect([...used].filter((p) => !known.has(p))).toEqual([]);
  });
});
