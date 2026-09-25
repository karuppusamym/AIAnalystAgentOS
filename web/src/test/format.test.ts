import { describe, expect, it } from "vitest";
import { errorMessage, ApiError } from "../api";
import { fmtMs, fmtNumber, fmtP, fmtPct, fmtUsd } from "../lib/format";
import { hypothesisIcon, toneFor } from "../lib/status";
import { parsePolicy } from "../pages/Governance";
import { cellStyle } from "../components/DashboardPreview";

describe("formatting & status", () => {
  it("formats numbers", () => {
    expect(fmtNumber(1234567)).toBe("1,234,567");
    expect(fmtNumber(null)).toBe("—");
    expect(fmtPct(0.1234)).toBe("12.3%");
    expect(fmtP(0.00001)).toBe("< 0.0001");
    expect(fmtP(0.04213)).toBe("0.0421");
    expect(fmtUsd(0.01234)).toBe("$0.0123");
    expect(fmtMs(1500)).toBe("1.5 s");
  });

  it("maps statuses to consistent tones", () => {
    expect(toneFor("COMPLETED")).toBe("success");
    expect(toneFor("verified")).toBe("success");
    expect(toneFor("RUNNING")).toBe("running");
    expect(toneFor("WAITING_USER")).toBe("warning");
    expect(toneFor("pending")).toBe("warning");
    expect(toneFor("rejected")).toBe("danger");
    expect(toneFor("failed_verification")).toBe("danger");
    expect(toneFor("superseded")).toBe("neutral");
    expect(toneFor("something-new")).toBe("neutral");
    expect(hypothesisIcon("supported")).toBe("✓");
    expect(hypothesisIcon("rejected")).toBe("✗");
  });

  it("validates policy JSON", () => {
    expect(parsePolicy('{"max_rows": 10}').value).toEqual({ max_rows: 10 });
    expect(parsePolicy("[1]").error).toMatch(/object/);
    expect(parsePolicy("{").error).toBeTruthy();
  });

  it("places dashboard cells on the 12-column grid", () => {
    expect(cellStyle({ chart: "a", row: 2, col: 6, width: 6, height: 4 })).toMatchObject({ gridColumn: "7 / span 6", gridRow: "3 / span 4" });
    expect(cellStyle({ chart: "a", row: 0, col: 10, width: 6, height: 1 }).gridColumn).toBe("11 / span 2");
  });

  it("renders validation errors with field locations", () => {
    const err = new ApiError(422, "invalid_input", "request validation failed", { errors: [{ loc: ["body", "email"], msg: "field required" }] });
    expect(errorMessage(err)).toBe("request validation failed: email field required");
  });
});
