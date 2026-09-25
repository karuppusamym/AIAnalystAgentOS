import { describe, expect, it } from "vitest";
import type { Hypothesis, Insight } from "../api";
import { layerLineage } from "../lib/lineage";
import { buildInvestigationTree, orphanFindings } from "../lib/tree";

const h = (over: Partial<Hypothesis>): Hypothesis => ({
  id: "h", workspace_id: "w", run_id: "r", code: "H1", question: "", statement: "s", spec: {}, priority: "medium",
  priority_score: 0.5, status: "proposed", methods: [], evidence: [], confidence: null, conclusion: null, parent_id: null,
  iteration: 1, origin: "agent", created_at: "", result: null, experiment_id: null, ...over,
});
const ins = (over: Partial<Insight>): Insight => ({
  id: "i", workspace_id: "w", run_id: "r", hypothesis_id: null, code: "I1", title: "t", finding: "f", confidence: 0.8,
  population_size: 0, business_impact: {}, caveats: [], evidence: [], verified: true, verification: {}, status: "verified",
  narrative_source: "template", created_at: "", ...over,
});

describe("investigation tree", () => {
  it("groups by question, nests follow-ups and attaches findings", () => {
    const hyps = [
      h({ id: "a", code: "H1", question: "Why slow?", priority: "low" }),
      h({ id: "b", code: "H2", question: "Why slow?", priority: "high" }),
      h({ id: "c", code: "H3", parent_id: "b" }),
      h({ id: "d", code: "H4" }),
    ];
    const tree = buildInvestigationTree("Objective", hyps, [ins({ id: "x", hypothesis_id: "c" }), ins({ id: "y" })]);
    expect(tree.map((q) => q.question)).toEqual(["Why slow?", "Objective"]);
    expect(tree[0].hypotheses.map((n) => n.hypothesis.code)).toEqual(["H2", "H1"]); // high priority first
    expect(tree[0].hypotheses[0].children[0].hypothesis.code).toBe("H3");
    expect(tree[0].hypotheses[0].children[0].findings.map((f) => f.id)).toEqual(["x"]);
    expect(orphanFindings(hyps, [ins({ id: "x", hypothesis_id: "c" }), ins({ id: "y" })]).map((i) => i.id)).toEqual(["y"]);
  });

  it("layers lineage upstream to downstream", () => {
    const layers = layerLineage({
      nodes: [{ type: "table", id: "t" }, { type: "dataset", id: "d" }, { type: "chart", id: "c" }],
      edges: [
        { from: ["chart", "c"], relation: "visualizes", to: ["dataset", "d"] },
        { from: ["dataset", "d"], relation: "reads", to: ["table", "t"] },
      ],
    });
    expect(layers.map((l) => l.map((n) => n.key))).toEqual([["chart:c"], ["dataset:d"], ["table:t"]]);
  });
});
