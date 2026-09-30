# CareerPulse Changelog

## 2026-09-22 — Phase 19: deployment review

- No new dependencies: `requirements.txt` untouched since Phase 0 (all
  phases used pre-existing fpdf2 / pypdf / beautifulsoup4).
- No new env vars: `.env.example` already covers GROQ_API_KEY_1/2/3,
  LLM_MODEL=openai/gpt-oss-120b, SESSION_SECRET, TEST_MODE/ALLOW_REAL_EMAIL.
- Supabase SQL staged in `schema.sql` (all with MANUAL ACTION blocks):
  MIGRATION A (RLS lockdown), MIGRATION B (resume_versions table),
  MIGRATION C (pending → draft), resume_locks_json column, MIGRATION D
  (pdf_page_target), plus the NULL-user_id investigation SELECT.
- Smoke-verified: app boots, new routes registered
  (`/pdf-pages`, `/approve`, `/resume/restore/{version_no}`).
- Pending user actions (unchanged): set SESSION_SECRET in Vercel; run the
  NULL-user_id listing SQL and share output; run staged migrations A–D +
  resume_locks_json in Supabase SQL Editor.

## 2026-09-22 — Phase 18: dead-code cleanup

- Removed stale `"pending"` defaults: `sync_app_to_supabase` and
  `sync_from_supabase_to_memory` fall back to `"draft"`; `Application.status`
  column default is now `"draft"`. (The two intentional transition-window
  references — workspace filter and submit gate — stay until MIGRATION C
  runs.)
- Delete route redirect is now status-aware (`sent` → `/sent`, everything
  else → workspace) instead of checking the dead `"pending"` label.
- User-facing wording updated: sent.html empty state points to the Workspace
  and the submit → approve → send steps; send dialog confirm button reads
  "Send Now" (was "Approve & Dispatch Now").
- pyflakes clean on all touched modules; workspace/approval/preview suites
  re-run green.

## 2026-09-22 — Phase 17: full test suite

- Ran all 17 test files: 16 pass fully. New coverage for Phases 11–16:
  test_preview (20), test_pdf_pages (16), test_email_format (17),
  test_workspace (12), test_approval_flow (24), test_version_history (16)
  — all green, plus all pre-existing suites (ATS recalibration, factuality,
  generation seams, Groq rotation, matching, optimization loop, readiness,
  resume editor, roles) still pass.
- Pre-existing failure (not touched, per scope): test_resume_parse.py's
  `test_extract_real_pdf` — the committed fixture
  `data/resumes/application_1.pdf` contains only ~36 chars of extractable
  text, so the parser correctly rejects it. Files pristine since Phase 2;
  needs a text-based sample PDF (Phase 2 fixture issue, out of scope).
- `test_deep_scan_51.py` is network-gated (Supabase) and was not run here;
  its Task 41 was updated in Phase 15 for the approval workflow and
  compile-checked.

## 2026-09-22 — Phase 16: version history UI (FR-R-03)

- Detail page has a Version History section: every recorded version
  (newest first, current marked), ATS score, timestamp, and a Restore
  button on older versions (with a confirm dialog).
- New `POST /application/{app_id}/resume/restore/{version_no}`: the
  restored AI content is the base; the user's current manual-edit locks
  are re-applied on top via `replace_regions()` — locks always win, so a
  restore never wipes the user's words (`ponytail:` comment in code).
  The restore re-renders the PDF (honoring the 1|2-page target),
  recomputes the ATS score, resets approval to ready (content changed),
  and is itself recorded as a new version — every restore is undoable.
  404 on missing version, 410 if the version file is gone, 403 for
  non-owners.
- New `tests/test_version_history.py` (16 checks): list UI, locks-win
  restore semantics, new-version bookkeeping, ATS recompute, approval
  reset, PDF re-render, 404/403.

## 2026-09-22 — Phase 15: explicit approval workflow

- Full state machine: `draft -> ready -> pending_approval -> approved -> sent`
  (+ `failed`). Intake creates `draft`; successful tailoring promotes to
  `ready` (failed tailoring stays `draft` — honest, never half-ready).
- New `POST /application/{app_id}/approve`: `pending_approval -> approved`
  only (409 otherwise). `submit-for-approval` tightened to `ready ->`
  `pending_approval` (legacy `pending` accepted as ready-equivalent during
  the MIGRATION C window).
- Send endpoint 409s unless status is exactly `approved` — nothing sends
  without explicit human approval, ever. `mark-applied` (external portal
  record-keeping) intentionally ungated.
- Content edits reset approval: email save, resume editor save, resume
  upload, and resume recreate move `pending_approval`/`approved` back to
  `ready` (drafts and sent rows untouched). New `_reset_approval_to_ready`
  helper.
- Detail page action bar is now step-aware: Draft shows a "not ready" note;
  Ready shows Submit for Approval; Pending approval shows Approve (+ "nothing
  sends automatically"); Approved shows Send; Sent shows state. The old
  combined "Approve & Send" one-click button is gone. Added `?approved=1`
  banner. The email card keeps only Save (its hint notes the reset).
- Updated `test_deep_scan_51.py` Task 41 to walk submit → approve → send
  (was a direct send); updated `test_preview.py` fixture to `ready`.
- New `tests/test_approval_flow.py` (24 checks): intake draft/ready,
  submit/approve/send gates incl. double-submit/double-approve 409s,
  edit/upload resets, sent-is-terminal, step-aware buttons.

## 2026-09-22 — Phase 14: workspace with status filter

- `/` is now a workspace (was "Pending Approvals"): clickable status
  cards (All / Draft / Ready / Pending approval / Approved / Sent /
  Failed) with live counts, a `?status=` filter, a Status column with
  color badges, and a friendly empty state per filter. Template still
  `approvals.html` (reused, no rename churn).
- Legacy `pending` rows (pre-MIGRATION C) are counted and listed under
  Draft so nothing is invisible during the migration window.
- New `tests/test_workspace.py` (12 checks): renders, filter cards,
  per-status filtering, legacy-pending-under-draft, invalid status falls
  back to all, empty-state copy.

## 2026-09-22 — Phase 13: application email prompt rewrite (§U.11)

- `write_application_email` prompt now mandates the §U.11 layout: greeting
  line (`Dear Hiring Manager,`, or `Dear [Name],` only when the JD names
  a real contact — never invent one), blank line, exactly 3 paragraphs
  (opening / technical depth / enthusiasm + CTA), then the mandatory
  sign-off block: resume-attached sentence, blank line, `Best regards,`
  + name/email/phone/LinkedIn on separate lines.
- The LinkedIn URL is pulled from `candidate.links` and passed to the
  model explicitly (`CANDIDATE LINKEDIN:`); the deterministic no-LLM
  fallback was rewritten to the same structure (it previously had no
  LinkedIn and only 2 paragraphs).
- Safety defaults re-verified in `app/config.py`: `TEST_MODE=True`,
  `ALLOW_REAL_EMAIL=False` (nothing can real-send in dev).
- New `tests/test_email_format.py` (17 checks): prompt carries every §U.11
  rule, LinkedIn reaches the model, fallback email conforms block by
  block, and it degrades cleanly with no LinkedIn on file. Live-LLM
  sample-JD validation still needs real Groq keys (not in this env).

## 2026-09-22 — Phase 12: true 1-page / 2-page PDF export (§U.8)

- `render_pdf_from_html(..., page_target=1|2)` (fpdf2 only, no new
  dependency). 1-page default: measured auto-fit scaling (1.45 → 0.88
  readability floor — tiny fonts banned); 2-page: natural scale with
  content flowing onto page 2 via fpdf2 auto page-break, condensing only
  if it would exceed 2 pages, never force-filled.
- Page-count enforcement is now real: new `PageOverflowError` replaces the
  old silent behavior (which emitted a clipped 1-page PDF when content
  overflowed even at minimum scale). Overflow now fails loudly with a
  message guiding the user to the 2-page variant; it propagates unwrapped
  (no Chrome fallback — that would produce an unmeasured PDF).
- Enforcement wired into all render paths: download route (422 on
  overflow), send route (blocks with a redirect error instead of
  attaching a clipped PDF), resume editor save (HTML + locks + version
  still saved; error surfaces at the end).
- Per-application page choice: new additive `Application.pdf_page_target`
  column (default 1; Supabase MIGRATION D staged in `schema.sql` — manual
  action required). Preview page has a 1|2 selector; switching deletes
  the cached PDF so the next render uses the new target.
- New `tests/test_pdf_pages.py` (16 checks, pypdf-verified): 1-page fit,
  overflow raises (not clips), 2-page flow onto page 2 with text intact,
  no forced fill, invalid target rejected, route-level choose/persist/
  400/403/404.

## 2026-09-22 — Phase 11: read-only preview step

- New `GET /application/{app_id}/preview`: the step between editing and
  approval/send. Shows the resume exactly as it will be sent (the PDF
  renderer's own output embedded via the existing `/resume.pdf` route —
  no duplicated representation), the ATS score with its traceable
  breakdown (parsing safety /40 + job relevance /60, matched/missing
  skills, warnings, suggestions — recomputed deterministically from the
  live resume file so it always matches what is previewed), and the
  JD↔KB gap report summary (coverage, matched/missing skills, top
  projects).
- Read-only by construction: no textareas, no email/resume edit fields,
  no contenteditable. Editing stays in the Phase-10 editor; the preview
  links back to it.
- The preview's action is `POST /application/{app_id}/submit-for-approval`
  (draft/ready → pending_approval; 409 from approved/sent, 403 for
  non-owners). The full state machine (approve step, send gating) lands
  in Phase 15; this route is deliberately narrow.
- New `tests/test_preview.py` (19 checks): render + all three sections,
  read-only assertions, 404/403, submit transition, 409 on terminal
  states, status reflected in the preview.
- "Preview" button added to the application detail header actions.

## 2026-09-22 — Phase 10: full-coverage resume editor + inviolable manual-edit locks (§U.13)

- The Section Customizer now covers every resume section: summary, skills,
  experience, projects, **education, certifications, links, and per-bullet
  editing** (project bullets and per-experience bullets, one textarea each).
  Save → rescore → display, as before.
- Certifications are now a real rendered section: `render_certifications_html()`
  builds it from the KB (`candidate.certifications`); the section (header
  included) disappears when empty. New `REGION:CERTIFICATIONS` in all three
  `se_*` templates; new `REGION:LINKS` wraps the header link items.
- Links editing: quick URL fields (LinkedIn/GitHub/Portfolio/Website) applied
  via `update_links_html()` on top of the current LINKS region, plus an
  advanced raw-HTML override. Existing brand icons are preserved.
- **Manual edits are inviolable locks.** Every region the user touches is
  stored as final region HTML in the new additive `Application.resume_locks_json`
  column (Supabase migration staged in `schema.sql` — manual action required).
  `tailor_application_resume(..., locked_regions=...)` re-applies locks on
  every loop iteration, so Recreate/AI-optimize never wipes what the user
  typed. Precedence: Phase-9 truthfulness/CS guardrails > user locks > AI.
- Manual saves now record a restorable resume version (FR-R-03), like
  generations do.
- New `tests/test_resume_editor.py` (30 checks): region edits, certs
  text→HTML, link href replace/append, per-bullet swaps, generic
  `extra_regions`, lock round-trip through the full tailor loop, guardrail
  precedence over a non-CS lock summary, direct builder overrides.

## 2026-09-22 — Phase 9: anti-hallucination / factuality (guardrails re-verified)

- Re-verified all truthfulness guardrails under the new Phase-8 loop
  (up to 5 iterations, custom instructions as locks): skills rendering stays
  strictly candidate-attested even with hostile `extra_target_skills`;
  projects/experience render only from the KB; LLM refinement `extra_skills`
  are filtered against the attested set; summaries pass through
  `build_safe_cs_summary` (CS-only, no-visa, CS-anchor or deterministic fallback).
- New `tests/test_factuality_probes.py` (22 checks): hostile JD through the
  full loop (no invented Rust/Haskell/Kubernetes/brain-surgery skills, no
  Google/OpenAI/NASA companies or CTO/brain-surgeon titles in experience);
  actively malicious LLM refinement payload (summary rejected, unattested
  skills filtered, attested kept, loop stays bounded); project-exclusion
  lock holds across all iterations; summary guardrail unit probes
  (visa terms, non-CS terms, too short, no CS anchor).
- Exit criterion met: zero invented skills/companies/titles across probe runs.

- Target retargeted 85 → 87 (`tailor_application_resume` defaults;
  `check_candidate_85_eligibility` → `check_candidate_87_eligibility`;
  KB badge now reads "87+ ATS Optimization: Active").
- **Custom instructions no longer silently disable the loop.** Previously any
  `custom_focus` with interpreted instructions skipped all iterations — this
  bit every Phase-5 review-flow application, because the JD↔KB gap report's
  `optimizer_focus` brief is passed as `custom_focus`. Now custom instructions
  are locked constraints (summary draft, forced/excluded projects, skill
  removals, category include/exclude filters) and the loop always runs.
- Loop: max 5 iterations; plateau stop (improvement < 1.0 for 2 consecutive
  attempts); always chains off the BEST build (previously chained off the
  latest even when worse); LLM budget ≤6 calls/application
  (1 interpret iff custom_focus + ≤4 refinements).
- `TailorResult` now carries per-attempt history (attempt, score, delta,
  gaps_closed, via llm/deterministic, note), `kb_snapshot_hash`,
  `target_reached`, `stop_reason`.
- Every generation persists a `resume_versions` row (new additive model +
  `record_resume_version()` in `app/db.py`): version_no, role, jd_text,
  kb_snapshot_hash, versioned HTML path (`application_<id>_v<n>.html`, so any
  version is restorable), pdf path, score, iteration count, score history,
  template id. Wired into both generation paths (`intake.create_application`
  and the recreate route). The staged Supabase migration in `schema.sql`
  (still unapplied remotely) was extended with `jd_text` and
  `score_history_json` to match.
- Manual portal applies no longer fake `ats_score=85.0`/`ats_attempts=1`;
  they record 0/0 (no resume generated, no evidence), keeping them out of
  the sent-metrics average.

All artificial score logic removed from `app/ats.py`. Every point is now
traceable to measurable evidence. Visible scores will shift downward on
resumes that previously benefited from free points — that is expected.

### Parsing safety (0–40)

| Component (max) | Old | New |
|---|---|---|
| Headings (8) | 8 × matched/5 headings — unchanged | unchanged (earned) |
| Contact (8) | 8 / 4 / 0 for both/either/neither — unchanged | unchanged (earned) |
| Dates (8) | 8 if ≥2 dates else **4.0 free** | 4 per date found, cap 8 (0 dates → 0) |
| Layout (16) | **16.0 flat grant** | 4 pts per evidence check: no table/column artifacts (`\|`, tabs, 4+ spaces); ≥3 bullet lines; ≥3 standard headings; content length 200–12000 chars |

### Job relevance (0–60)

| Component (max) | Old | New |
|---|---|---|
| Required keyword coverage (26) | 26 × matched/total, but **22.0 free when the JD shared zero keywords** with attested skills | 26 × matched/total; **0.0** on zero JD hits |
| Preferred keywords (6) | **5.0 flat grant** | 1.5 pts per distinct tech term present in *both* JD and resume (excluding required skills, no double count), cap 6 |
| Title alignment (8) | **4.0 baseline**, 8 if ≥50% title words, 6 if any | 0 baseline; 8 if ≥50% title words, 6 if any, else 0 |
| Experience & education (8) | 8 if experience section else **4.0 fallback** | 4 for an experience section + 4 for an education section, each earned separately |
| Semantic similarity (6) | 6 × cosine(resume, JD) — unchanged | unchanged (earned) |

### Notes

- `evaluate_relevance` stays fully deterministic and offline (no LLM); the
  preferred-keyword vocabulary is the shared `TECH_VOCAB` from `app/matching.py`.
- New warnings/suggestions name exactly what is missing (dates, bullets,
  education section, …) so the user knows how to earn the points.
- The 87+ optimization loop (Phase 8) targets the recalibrated scale.

## 2026-09-22 — Phases 0–6

- **Phase 0**: security/data-ownership fixes (DB-only auth, deny-by-default
  ownership, session hardening, no tracebacks in 500s, test-safe email
  defaults, RLS + additive migrations staged in `schema.sql` for the owner
  to run).
- **Phase 1**: Groq 3-key rotation (`GROQ_API_KEY_1/2/3`, round-robin,
  per-key quarantine, 429/5xx failover, `openai/gpt-oss-120b` primary).
- **Phase 2**: resume PDF upload → text extraction (pypdf) → structured
  parse → KB import review (saves nothing by itself).
- **Phase 3**: KB readiness gating (blocked/minimum/strong) + Education and
  Certifications sections in the KB and resume templates.
- **Phase 4**: role detection (`app/roles.py`) + selection state machine;
  Generate stays disabled until ROLE_RESOLVED (user pick or AI pick).
- **Phase 5**: JD↔KB gap report (matched/missing skills, project relevance)
  shown on a review screen before generation; feeds the optimizer.
- **Phase 6**: generation split into content (`build_resume_sections`) and
  rendering (`render_resume`) halves + `resolve_template()` registry seam;
  behavior unchanged.
