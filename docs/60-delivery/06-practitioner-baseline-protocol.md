# Paired practitioner baseline — protocol (P4-08)

Defined 2026-09-27. **Not run yet.** Running it needs qualified human analysts, an engineer and an
ML practitioner, and an independent scorer; none were available to the session that wrote it. No
statement about human effort, time saved or relative quality may cite this protocol until a dated
results file exists under `docs/60-delivery/evidence/`.

It implements [evaluation plan](05-evaluation-plan.md) §4 on the held-out corpus
(`evaluation/heldout/corpus.yaml`). The automated side is `scripts/benchmark_heldout.py`; the paired
comparison is `evaluation/heldout/baseline.py`; the results sheet is
`evaluation/heldout/baseline_template.csv`.

## 1. What is compared

The same task, the same data, the same permissions and the same time window, done two ways:

| Arm | Who does the work | Human effort counted |
|---|---|---|
| `analystos` | AnalystOS, operated by a person who briefs it, reviews its output and corrects it | briefing, reviewing, correcting and re-running minutes of that person |
| `practitioner` | A qualified practitioner with their usual tools (SQL client, notebook, spreadsheet, BI) and no AnalystOS | all their working minutes, including their own checking |

A task's output is what a business owner would receive: findings with numbers, a published table,
or a model verdict, or an explicit "cannot be answered from this data, because …".

## 2. Tasks

* Use at least **20 paired tasks** (plan §4). Draw them from the held-out corpus with the same family and
  domain mix as the automated run (at least 6 analysis, 4 engineering, 4 ML and 4 abstain tasks), plus
  pilot tasks the owner supplies when there are any.
* A task pack is: the brief (`objective`), the data as the platform sees it (the CSV files the platform tier
  uploads; the generator and seed are recorded, never given), the allowed actions, and a deadline.
* The rubric (`rubric` in `corpus.yaml`) is **never** shown to either arm or to the operator.
* The practitioner may not see AnalystOS output for that task, and the operator may not see the
  practitioner's.

## 3. Participants

* Record, for each practitioner, the role and years of relevant experience (no names in the sheet: a
  pseudonymous `participant` id). A practitioner does each task at most once.
* The AnalystOS operator must not be a developer of the platform where that can be avoided; if it cannot,
  say so in the results file.
* The scorer is a third person who wrote neither the rubric nor either output.

## 4. Time capture

* Each attempt records `started_at` / `finished_at` (UTC) and minutes in three buckets:
  `active_minutes` (doing the task), `review_minutes` (checking an output before calling it done),
  `correction_minutes` (fixing something found in review, including AnalystOS re-runs).
* Time is captured by the participant with a visible timer and checked against tool logs where they
  exist (AnalystOS audit and run timestamps; practitioner query history). Breaks are excluded.
* `interventions` counts every time the operator had to step in on AnalystOS (edit a spec, answer a
  clarification, re-run, override). The practitioner arm records 0.
* Model and infrastructure cost go in `model_usd` and `infrastructure_usd`; unknown cost is left blank,
  never written as 0.

## 5. Blinded scoring

1. The coordinator runs `evaluation.heldout.baseline.assign_codes(task_ids, seed=<recorded seed>)` and keeps
   the code map private.
2. Each output is rendered to a neutral format (markdown text plus any table as CSV; no product styling,
   no tool names, no timestamps) and filed under its code.
3. The scorer receives the rubric and the coded outputs only, and records one verdict per output using the
   runner's statuses: `accepted`, `correct_abstention`, `confident_wrong`, `incomplete`,
   `unnecessary_abstention`, `wrong_abstention` (or `error` when there is no output). Every
   `confident_wrong` needs a note naming the wrong claim.
4. The coordinator joins verdicts back to arms. Where the scorer's verdict for the AnalystOS arm differs
   from the automated runner's status for the same task, both are reported; the scorer's verdict counts.

## 6. Analysis

`baseline.paired_summary(baseline.load(path))` reports, per arm: quality (accepted or correctly abstained /
scored), confident-wrong count, median human minutes and interventions; and over tasks both arms completed:
the median minutes saved with a bootstrap 95% interval, the median effort ratio (AnalystOS / practitioner),
the tasks where the arms disagree on acceptability, and whether the plan's target holds (**at least 50%
less human effort with no lower accepted quality**).

This is a pilot estimate on a small sample: report every task, the disagreements and the interval, and do
not claim general superiority from it (plan §4).

## 7. Results template

Fill `evaluation/heldout/baseline_template.csv` (one row per task and arm) and publish a dated file
`docs/60-delivery/evidence/<date>-practitioner-baseline.md` with this shape:

```markdown
# Practitioner baseline (P4-08) — <date>

Corpus version <n> (sha256 <…>), AnalystOS commit <sha>, models <off | provider/model>.
Participants: <k> practitioners (<roles, experience>), operator <role>, scorer <role>. Blinding seed <s>.

| Measure | AnalystOS | Practitioner |
|---|---|---|
| tasks scored | | |
| quality (accepted or correct abstention) | | |
| confident-wrong | | |
| median human minutes per task | | |
| interventions | | — |
| model USD / infrastructure USD | | |

Paired tasks: <n>. Median minutes saved: <x> (95% CI <a>–<b>). Median effort ratio: <r>.
Plan target (≥ 50% less effort, no lower quality): <met | not met | not determinable>.

| Task | AnalystOS verdict | minutes | Practitioner verdict | minutes | notes |
|---|---|---|---|---|---|

Disagreements: <tasks and why>. Deviations from the protocol: <none | …>.
```

## 8. What stays open until it is run

* The tracker row P4-08 cannot claim the paired-baseline evidence until the file above exists.
* Recruiting participants and a scorer is an owner action.
