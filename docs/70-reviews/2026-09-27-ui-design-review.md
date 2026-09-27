# UI design review before the demo

Date: 2026-09-27. Base: `c288403`. Scope: visual and interaction design and consistency of every screen in
`web/src/routes.ts`, not new features. This review is dated evidence; status stays in
`docs/60-delivery/01-tracker.md`.

## Method

- The mocked Playwright backend (`web/e2e/fixtures.ts`, `src/test/mockBackend.ts`) served the production bundle
  (`vite preview`).
- A capture script (not committed) signed in through the real login form and took full-page screenshots of each
  screen and each tab: 18 routes, 8 Catalog tabs, 7 Work tabs (plus a Data Thread with a selected investigation),
  9 Outputs types, 3 Monitoring tabs, the investigation, the agent console and the finding. That is 44 views.
- Each view was captured at 1440×900 and 390×844, in light and dark, as admin and as analyst.
- Two extra variants covered the other states:
  - `empty`: a fresh workspace with no objective and every list empty. This shows the first-run checklist and the
    empty states.
  - `broken`: every workspace read returns 500. This shows the error states.
- Screenshot names are `<variant>-<theme>-<viewport>-<view>.png`, for example `admin-light-desktop-overview.png`.
  There are 436 per pass, kept outside git.
- A script scanned every phone-width view for elements wider than the viewport that no scroll container clips.
- Contrast was checked by the axe checks already in `e2e/journeys.spec.ts` (WCAG 2.1 A/AA and best practice), in
  light and dark.

Scale: **P1** must be fixed for the demo, **P2** should be, **P3** later.

## Findings

### P1

| # | Finding | Where (screenshots) | Status |
|---|---|---|---|
| P1-1 | Status words were inconsistent. Run statuses showed the raw upper-case enum (`COMPLETED`) while other badges were lower case (`verified`, `ready`). Underscore enums leaked into badges (`waiting_user`, `failed_verification`, `rolled_back`). | `admin-light-desktop-investigation`, `admin-light-desktop-work-investigations`, Managed tables | Fixed |
| P1-2 | At phone width, three screens scrolled sideways: Work → Investigations (screen-reader text inside the table escaped the table's scroll box), Members & policy, and Catalog → Review queue (a fields table with no scroll wrapper). | `admin-light-mobile-work-investigations`, `admin-light-mobile-policy`, `admin-light-mobile-catalog-review` | Fixed |
| P1-3 | On Members & policy, the two approval checkboxes did not wrap, so their labels and hints ran past the card's right edge (at desktop width too). | `admin-light-desktop-policy` | Fixed |
| P1-4 | In the first-run checklist, the one next-step button ("Connect data") stretched across the whole card. It read like a banner, not a button. | `empty-light-desktop-overview` | Fixed |
| P1-5 | On the Overview, the "What needs you" hints (and the Usage stat hints) used the purple decision-model colour. They looked like links and pulled the eye from the counts. | `admin-light-desktop-overview`, `admin-light-desktop-usage` | Fixed |
| P1-6 | For an owner, the Overview's largest block was the "Workspace work modes" configuration form. That is setup, not something that needs the person, and it is the first thing a demo audience sees after sign-in. | `admin-light-desktop-overview`, `admin-dark-desktop-overview` | Fixed |
| P1-7 | A finding's "How it was checked" table showed the verifier's raw codes (`method_fit`, `significance_after_bh`, `representative_populatio n`, broken mid-word) in the primary UI. | `admin-light-desktop-finding` | Fixed |

### P2

| # | Finding | Where | Status |
|---|---|---|---|
| P2-1 | Frameless buttons (`btn-ghost`) were bold grey text: "Data Thread", "Agent console", "Open", "Remove", "Versions", "Pin", "Fork from here", "Why this number?". They read as labels next to the framed buttons in the same row. | `admin-light-desktop-work-thread-run`, `admin-light-desktop-registry`, `admin-light-desktop-investigation` | Fixed: accent colour and hover tint. Top-bar icons stay neutral. |
| P2-2 | A finished investigation still showed Pause, Resume and Cancel (all disabled) and "Stream closed" (jargon) in its header. | `admin-light-desktop-investigation` | Fixed: the controls show only while it runs, Pause and Resume swap, and the indicator says "Not live". |
| P2-3 | The hypothesis board scrolled sideways inside the investigation, so the "Rejected" and "Inconclusive" columns were cut off at desktop width and on phones. | `admin-light-desktop-investigation`, `admin-light-mobile-investigation` | Fixed: columns wrap onto the next row. |
| P2-4 | The Outputs empty state for a fresh workspace said "Choose another investigation or output type" beside an empty "Select an output" pane. It gave no next step. | `empty-light-desktop-outputs` | Fixed: "No outputs yet" with Start work. `EmptyState` gained an `action`. |
| P2-5 | An empty Approval inbox showed three empty panes: the empty state, an empty queue and "No proposal selected". | `empty-light-desktop-approvals` | Fixed: one empty state, plus "Show decided approvals" when history exists. |
| P2-6 | On a pending approval, the proposed content and payload diff was folded away, although it is the thing being approved. | `admin-light-desktop-approvals` | Fixed: open while pending, folded once decided. This also restores the `approval inbox shows the payload diff` journey. |
| P2-7 | Counts were not pluralised: "1 sources", "1 investigations", "1 queries, 0 tests", "1 workspace or data preparation outputs are shown in All outputs". | `admin-light-desktop-workspaces`, `admin-light-desktop-finding`, `admin-light-desktop-outputs` | Fixed; the Outputs sentence was rewritten. |
| P2-8 | On the Workspaces card, "Disable workspace" was the first framed button, a peer of "Data inventory". | `admin-light-desktop-workspaces` | Fixed: it is last and styled as a red text action. |
| P2-9 | The agent console named its investigation by raw id (`run_demo`). | `admin-light-desktop-console` | Fixed: it shows the investigation's question, with the id in the tooltip. |
| P2-10 | Notices broke at every `<strong>`, for example "Version **3**" on its own line and then "is in effect…". | `admin-light-desktop-platform` | Fixed: notices use block flow. |
| P2-11 | A long page subtitle pushed the header action below it, so "New schedule" sat under the description. "What would change" stretched full width. | `admin-light-desktop-schedules` | Fixed |
| P2-12 | Output list rows cut names short ("p1_incidents_clea") while their type label wrapped onto two lines. | `admin-light-desktop-outputs` | Fixed: the type label keeps one line and the name takes the space. |
| P2-13 | On phones, the top bar showed a bare "Ctrl K" chip as the search button, and Work's seven tabs wrapped into three rows above the content. | `admin-light-mobile-overview`, `admin-light-mobile-work-investigations` | Fixed: the button reads "Go to…" and the tabs scroll in one row. |
| P2-14 | The investigations table squeezed the question to one or two words per line on phones ("Why are / P1…"). | `admin-light-mobile-work-investigations` | Fixed: the question column keeps a readable width, and the table scrolls. |
| P2-15 | Data → Definitions opens on "Data model", so the semantic graph is no longer on screen by default. `?tab=graph` legacy links also land there. The graph and edge-table checks fail at base: vitest `knowledge.test.tsx › lists governed and inferred edges` and the Playwright axe journey `Data · definitions` (light and dark). | `admin-light-desktop-catalog-definitions` | Left for the functional track. The failure predates this review, and the fix is a navigation choice. |
| P2-16 | The Sources page title ("Sources & crawls") differs from its nav label ("Sources"), and the secret reference (`env:SN_PASSWORD`) sits in the primary line. | `admin-light-desktop-sources` | Open |
| P2-17 | Hypothesis cards and the finding's "Verify" row show method codes (`mann_whitney`, `rank_biserial`, `welch_t`) in the primary UI. | `admin-light-desktop-investigation`, `admin-light-desktop-finding` | Open. The codes are the evidence vocabulary; a plain-words pass needs the method registry's labels. |

### P3

| # | Finding | Where |
|---|---|---|
| P3-1 | Nav areas with a single screen repeat their heading: Overview / Overview, Work / Work, Outputs / Outputs. | every workspace screen |
| P3-2 | Catalog → Metrics puts the "Propose a KPI" form above the KPI list, so the list is below the fold. | `admin-light-desktop-catalog-metrics` |
| P3-3 | Alert actions use three different fills (Acknowledge neutral, Resolve green, Investigate blue). | `admin-light-desktop-monitoring-alerts` |
| P3-4 | Platform settings history shows "By `usr_admin`" (an id, not a name). The approval payload summary says "[1 items]". | `admin-light-desktop-platform`, `admin-light-desktop-approvals` |
| P3-5 | The Members & policy audit switch renders "This Workspace / Whole Platform" in title case (`.seg-btn` capitalises). | `admin-light-desktop-policy` |
| P3-6 | The Data Thread step "no verdict recorded (recorded)" repeats itself. The merge row squeezes its "Report title" label. | `admin-light-desktop-work-thread-run` |
| P3-7 | Costs show four decimals everywhere (`$0.0421`, `$0.1200`). This suits run costs but is noisy on Usage totals. | `admin-light-desktop-usage` |
| P3-8 | Analysis contexts without a name list as "v · unknown". | `admin-light-desktop-catalog-contexts` |

### Checked and fine

- Focus rings are the global `:focus-visible` 2 px outline and are visible in both themes.
- Axe reports no contrast violations in light or dark on any journey screen, including after the ghost-button
  colour change.
- Error states render the server's message with Retry (`broken-*`).
- Loading uses one spinner component.
- The sidebar ending at 900 px in full-page shots is an artefact of `position: sticky; height: 100vh` in a
  full-page capture, not a layout bug.

## Before and after, per P1

- **P1-1:**
  - Before: `COMPLETED` (upper case) beside `verified`, and `waiting_user`, `failed_verification` and `rolled_back`
    as written by the API.
  - After: `StatusBadge` shows `statusLabel(status)`, lower case with underscores as spaces ("completed",
    "rolled back"), with plain phrases where a word needs them ("waiting for you", "not verified").
  - The raw value stays in `data-status`, and the tone is unchanged. One component change covers every badge in the app.
- **P1-2:**
  - Before: page `scrollWidth` 588 px (Work), 673 px (policy) and 499 px (review queue) on a 390 px phone.
  - After: none of the 44 views at phone width is wider than the viewport.
  - How: `.table-wrap` is `position: relative` (absolute `sr-only` text stays inside its scroll box), the review
    fields table sits in a `.table-wrap`, and `.main` has `overflow-x: clip` as a backstop. `clip` keeps the
    sticky panels working.
- **P1-3:**
  - Before: "Publishing needs an approval Recommended; the platform may require it regardless." on one line,
    past the card edge.
  - After: a `.check-row` grid with the checkbox, the label, and the hint underneath, wrapping inside the card.
- **P1-4:**
  - Before: "Connect data" was a 1,070 px wide bar.
  - After: a normal primary button aligned with its step text. `.checklist-body` uses `justify-items: start`; the
    goal form still spans the width.
- **P1-5:**
  - Before: purple hint lines under each count.
  - After: muted secondary text (`--text-3`) with a line-length cap, so the counts lead.
- **P1-6:**
  - Before: an open "Workspace work modes" card, with a fieldset, three checkboxes and "Review changes", under
    "What needs you".
  - After: a closed "Work modes" disclosure, like "At a glance".
  - The form and its tests are unchanged. The Workspaces card's "Work modes" button still opens it in full.
- **P1-7:**
  - Before: `method_fit`, `significance_after_bh`, and `representative_populatio` / `n` wrapped mid-word.
  - After: "Method fits the question", "Significant after multiple-test correction", "Population is
    representative" and so on. The code stays in the cell's tooltip, and the detail column is unchanged.

After-screenshots have the same names as the before set. The P1 before and after pairs are
`admin-light-desktop-investigation`, `admin-light-mobile-work-investigations`, `admin-light-mobile-policy`,
`admin-light-desktop-policy`, `empty-light-desktop-overview`, `admin-light-desktop-overview` and
`admin-light-desktop-finding`.

## Tests whose wording changed on purpose

- `src/test/components.test.tsx` › StatusBadge: expects "waiting for you" (was `WAITING_USER`) and asserts
  `data-status`. A second case checks one case and no underscores.
- `src/test/pipelines.test.tsx` › rollback: expects the badge "rolled back" (was `rolled_back`).

`SCREEN_BUDGET`, routes, first-login concepts and API contracts are unchanged. `conceptCount`, `ia` and `lightIa`
pass.
