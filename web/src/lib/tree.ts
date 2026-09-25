import type { Hypothesis, Insight } from "../api";

export interface HypNode {
  hypothesis: Hypothesis;
  findings: Insight[];
  children: HypNode[];
}

export interface QuestionNode {
  question: string;
  hypotheses: HypNode[];
}

const PRIORITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };

function byPriority(a: HypNode, b: HypNode): number {
  const pa = PRIORITY_RANK[a.hypothesis.priority] ?? 3;
  const pb = PRIORITY_RANK[b.hypothesis.priority] ?? 3;
  if (pa !== pb) return pa - pb;
  if (b.hypothesis.priority_score !== a.hypothesis.priority_score) return b.hypothesis.priority_score - a.hypothesis.priority_score;
  return a.hypothesis.code.localeCompare(b.hypothesis.code, undefined, { numeric: true });
}

/**
 * Question -> Hypotheses (follow-ups nested under their parent) -> Findings.
 * Hypotheses without a question fall under the run objective.
 */
export function buildInvestigationTree(objective: string, hypotheses: Hypothesis[], insights: Insight[]): QuestionNode[] {
  const nodes = new Map<string, HypNode>();
  for (const h of hypotheses) {
    nodes.set(h.id, { hypothesis: h, findings: insights.filter((i) => i.hypothesis_id === h.id), children: [] });
  }
  const roots: HypNode[] = [];
  for (const n of nodes.values()) {
    const parent = n.hypothesis.parent_id ? nodes.get(n.hypothesis.parent_id) : undefined;
    if (parent && parent !== n) parent.children.push(n);
    else roots.push(n);
  }
  const sortRec = (list: HypNode[]) => {
    list.sort(byPriority);
    list.forEach((n) => sortRec(n.children));
  };
  const questions = new Map<string, HypNode[]>();
  for (const r of roots) {
    const q = r.hypothesis.question?.trim() || objective;
    if (!questions.has(q)) questions.set(q, []);
    questions.get(q)!.push(r);
  }
  const out = [...questions.entries()].map(([question, hyps]) => {
    sortRec(hyps);
    return { question, hypotheses: hyps };
  });
  if (out.length === 0) return [{ question: objective, hypotheses: [] }];
  return out;
}

/** Insights not attached to any hypothesis in the run (still shown so every finding is reachable). */
export function orphanFindings(hypotheses: Hypothesis[], insights: Insight[]): Insight[] {
  const ids = new Set(hypotheses.map((h) => h.id));
  return insights.filter((i) => !i.hypothesis_id || !ids.has(i.hypothesis_id));
}
