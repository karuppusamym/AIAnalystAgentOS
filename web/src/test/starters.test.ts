/** Ask's starter questions and example SQL come from the catalog alone: any database, no model call. */
import { describe, expect, it } from "vitest";
import type { CatalogAsset, CatalogColumn } from "../api";
import { entityOf, exampleSql, primaryTable, starterQuestions } from "../lib/starters";

const col = (name: string, over: Partial<CatalogColumn> = {}): CatalogColumn => ({
  name, data_type: "text", business_name: null, description: null, tags: [], tags_origin: "crawler", role: null,
  unit: null, pii: null, glossary: null, ...over,
});

const asset = (over: Partial<CatalogAsset>): CatalogAsset => ({
  id: "a", fq: "sales.orders", source_id: "s", name: "orders", business_name: null, description: null,
  description_origin: null, reviewed: false, selected: true, lifecycle: "active", row_count: 1000, role: "fact",
  domain: "sales", grain: null, confidence: 0.8, last_crawled_at: null, columns: [], ...over,
});

const ORDERS = asset({
  columns: [
    col("order_id", { role: "identifier", is_key: true, profile: { distinct: 1000, null_rate: 0 } }),
    col("customer_email", { role: "contact", tags: ["pii"], profile: { distinct: 900, null_rate: 0 } }),
    col("status", { role: "dimension", profile: { distinct: 4, null_rate: 0 } }),
    col("region", { role: "dimension", business_name: "Sales region", profile: { distinct: 6, null_rate: 0.01 } }),
    col("amount", { role: "amount", data_type: "numeric", profile: { distinct: 800, null_rate: 0 } }),
    col("ordered_at", { role: "timestamp", data_type: "timestamp", profile: { distinct: 990, null_rate: 0 } }),
  ],
});

describe("starter questions", () => {
  it("asks about the fact table's own entity, time, dimensions and measures", () => {
    const qs = starterQuestions([asset({ name: "customers", role: "dimension", row_count: 5000 }), ORDERS]);
    expect(qs).toEqual([
      "How many orders are there?",
      "How many orders per month?",
      "Which status has the most orders?",
      "What is the average amount by status?",
    ]);
    for (const q of qs) expect(q.split(" ").length).toBeLessThanOrEqual(12);
  });

  it("never builds on sensitive columns, keys or unselected tables", () => {
    const all = JSON.stringify([starterQuestions([ORDERS]), exampleSql([ORDERS])]);
    expect(all).not.toMatch(/email|order_id/);
    expect(starterQuestions([asset({ selected: false, columns: ORDERS.columns })])).toEqual([]);
  });

  it("names the entity from the table", () => {
    expect(entityOf(asset({ name: "fact_order_lines" }))).toBe("order line");
    expect(entityOf(asset({ name: "incident", business_name: "Incident" }))).toBe("incident");
    expect(entityOf(asset({ name: "categories" }))).toBe("category");
    expect(primaryTable([asset({ id: "d", role: "dimension", row_count: 9e6 }), ORDERS])?.id).toBe("a");
  });

  it("writes read-only example SQL over real columns", () => {
    const ex = exampleSql([ORDERS]);
    expect(ex[0]).toEqual({ label: "Count orders", sql: "SELECT COUNT(*) AS orders FROM sales.orders" });
    expect(ex.map((e) => e.label)).toContain("Orders by status");
    expect(ex.every((e) => /^SELECT /.test(e.sql))).toBe(true);
  });
});

describe("starter questions without a profile (names only)", () => {
  const INCIDENT = asset({ name: "incident", fq: "stg.incident", columns: [
    col("sys_id", { role: "identifier" }), col("number", { role: "dimension" }), col("opened_at", { role: "timestamp" }),
    col("priority", { role: "dimension" }), col("assignment_group", { role: "foreign_key" }),
    col("assignment_group_name", { role: "name" }), col("caller_id_name", { role: "name", tags: ["pii"] }),
    col("reassignment_count", { role: "measure", data_type: "integer" }),
  ] });

  it("skips identifier-like names and ranks by a key's label", () => {
    expect(starterQuestions([INCIDENT])).toEqual([
      "How many incidents are there?",
      "How many incidents per month?",
      "Which assignment group has the most incidents?",
      "What is the average reassignment count by assignment group?",
    ]);
  });
});

it("never averages a numeric code and ranks by a small labelled grouping", () => {
  const a = asset({ name: "incident", columns: [
    col("priority", { role: "dimension", semantic_type: "numeric", profile: { distinct: 5, null_rate: 0 } }),
    col("cmdb_ci", { role: "foreign_key" }), col("cmdb_ci_name", { role: "name", semantic_type: "categorical", profile: { distinct: 40, null_rate: 0 } }),
    col("assignment_group", { role: "foreign_key" }),
    col("assignment_group_name", { role: "name", semantic_type: "categorical", profile: { distinct: 12, null_rate: 0.03 } }),
    col("reassignment_count", { role: "measure", semantic_type: "numeric", profile: { distinct: 10, null_rate: 0 } }),
  ] });
  const qs = starterQuestions([a]);
  expect(qs).toContain("Which assignment group has the most incidents?");
  expect(qs.join(" ")).not.toMatch(/average priority/);
  expect(qs).toContain("What is the average reassignment count by assignment group?");
});
