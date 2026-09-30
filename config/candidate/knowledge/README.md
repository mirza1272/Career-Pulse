# Candidate Knowledge Base

This folder is the portable "who am I" module the agent draws on when tailoring a
resume. Swapping to a different person = replace the files here (plus
`../profile.yaml` and the LaTeX templates in `resumes/templates/latex/`).

## Files

| File | What it holds | Who reads it |
| --- | --- | --- |
| `../profile.yaml` | Identity, contact, links, skills, education, experience, `experience_years` | Matching engine, evidence validator, resume header/experience regions |
| `projects.yaml` | **Project library** — every project you can honestly claim | Project selector (picks the best N per job → PROJECTS region) |
| `portfolio.yaml` | Portfolio URLs and highlighted work | Application forms, resume links |

## The rule that keeps it honest

The agent **never invents** skills, projects, employers, or numbers. It can only:

- mention skills listed in `profile.yaml → skills`,
- render projects that exist in `projects.yaml`,
- keep header / contact / education **locked** (never edited).

Only three regions are ever rewritten per job: **SKILLS**, **PROJECTS**, and
**EXPERIENCE** bullet emphasis (plus the one-line SUMMARY). Everything else is
byte-for-byte preserved and verified after generation.

## How project selection works

For each job the agent:

1. filters `projects.yaml` to the ones eligible for the chosen resume variant
   (`domains: [ai_ml]` / `[full_stack]` / both),
2. scores each by skill/tag overlap with the job,
3. takes the top few (ties broken by `priority`, 1 = strongest),
4. drops their `bullet` lines into the PROJECTS region.

So to change which projects show up, edit `priority`, `skills`, `tags`, or
`domains` here — no code change needed.

## Review checklist (before going live)

- [ ] `profile.yaml` links (GitHub / LinkedIn / portfolio) are real
- [ ] `experience_years` reflects how you count it
- [ ] Education dates are correct
- [ ] Every project in `projects.yaml` is one you can defend in an interview
- [ ] `portfolio.yaml` URLs are real (remove `TODO(review)` markers)
