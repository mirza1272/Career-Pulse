# Resume delivery and dashboard fixes — 21 October 2026

Work applies to `careerpulse-latest`.

| Task | Result |
| --- | --- |
| Mobile PDF compatibility | Corrected duplicate internal font names across regular/bold/italic assets; PDFs embed distinct TrueType fonts and explicitly open at full-page fit. Raster rendering at normal zoom verified. A physical phone/email viewer still needs confirmation. |
| Download original PDF | Download buttons now request an attachment response. Preview, download and send resolve the same selected PDF. Uploaded and sent PDFs are not silently regenerated when missing. Cloud recovery retrieves the saved object instead. |
| Contact and Portfolio spacing | Contact items wrap as complete units in the PDF; Monarch reserves separate name/contact columns. All resume HTML templates reflow on small screens. |
| Automatic font and page fitting | Fitting uses finer size steps, keeps body/bullet text at least 9pt and respects the selected page limit in download, send, edit and restore paths. Overflow stops dispatch rather than sending an old or missing attachment. |
| Remove dashboard follow-up section | Removed the follow-up attention panel and its unused dashboard queries. Existing email activity history remains. |

Connected fixes:

- Sending preserves the uploaded custom PDF instead of replacing it with generated content.
- Repeated send requests on an already-sent application are rejected.
- Changing page count cannot replace a custom PDF and invalidates approval for generated resumes.
- Legacy HTML file pointers are rendered to PDF rather than returned as a fake PDF download.
- Fonts are served from `/assets`, and HTTP previews use HTTP font URLs with compatible CSP rules.
- Disk version history loads even without cloud configuration.
- Email configuration is refreshed once at dispatch start, preserving the same guard settings throughout the send.

Verification:

- Full suite: **131 passed** in an isolated copy without `.env` or live API credentials.
- The original tree reproduced both existing failures before they were repaired.
- Chromium: login, dashboard, download button, 320px/390px resume layouts, all five template previews, real font loading, browser console checks, and the dashboard/intake/editor/preview/templates/knowledge-base pages at 320px without horizontal overflow.
- Downloaded PDF starts with the PDF signature; regressions compare the selected file, cloud recovery and email attachment bytes.
- PDF checks cover distinct embedded font names, complete contact labels, sparse/dense content, page count and the 9pt readability floor.
- Additional regression checks cover ownership, missing uploads, failed rendering, duplicate dispatch, static fonts and dashboard panel removal.
- No live emails were sent. Cloud object recovery was tested with mocked HTTP responses; remote storage configuration was not changed.

Follow-up visual export correction:

- Header/social icons and project-link icons are now embedded as SVG vector paths in the PDF, with balanced contact spacing and clean title/icon/subtitle punctuation.
- Inline emphasis no longer joins adjacent words; unsupported nonbreaking hyphens are normalized instead of disappearing.
- The application editor embeds the actual PDF, matching download and email output. The separate HTML version remains available.
- A renderer revision refreshes outdated unsent generated PDFs from their saved HTML; sent and uploaded PDFs retain their original bytes.
- Added regression checks for actual PDF vector drawing commands, editor/download identity, email attachment byte identity, draft cache refresh and sent-file preservation.
- Verified the real PDF inside Chrome's built-in viewer at desktop/mobile widths, and visually inspected a corrected export of the existing resume. No live email was dispatched.

Existing email attachments cannot change. Newly exported generated PDFs include the rendering fixes.

Knowledge-base skills and startup sync correction:

- Generated skill priorities only rank existing KB skills; AI/JD suggestions cannot add claims.
- Display capitalization keeps established names such as Python, FastAPI and PyTorch. Related ATS terms no longer rename skills into different tools (for example, data validation into Pydantic).
- An empty user KB stays empty instead of falling back to the local YAML profile.
- Startup hydrates Supabase data independently of email test mode. When cloud configuration exists, offline admin seeding is skipped, including after a connection failure.
- Latest related regression run: **87 passed**, including PDF output, hostile skill suggestions, empty KB, startup failure, user isolation, readiness, custom focus, imports and resume delivery. The earlier 131-test run predates this follow-up.
- Audited the updated local startup: five Supabase GET requests and zero cloud writes. Chromium at 1440px and 390px confirmed local displayed experiences/skills equal the current cloud record, with no JavaScript errors.
- No cloud profile restoration or schema changes were performed; no live email was sent.

Knowledge-base Save confirmation correction:

- Email test mode no longer suppresses user/profile synchronization. No mock-type or email-domain exception bypasses the production save path.
- Cloud success requires a returned record matching the user ID and submitted KB. A failed or mismatched response is not reported as success.
- KB Save synchronizes before committing the working copy and updates the profile timestamp. Failure rolls back the working copy and renders HTTP 503 with submitted form values preserved for retry.
- When cloud configuration is missing, serverless saves fail visibly; offline local sessions receive an explicit temporary-session notice.
- Verification: **99 passed** in a copy without live credentials, including new cloud acknowledgement, failure, retry, test-mode, timestamp, missing-config and cache-loss checks plus existing resume, isolation and readiness regressions.
- Chromium desktop and 390px mobile: failed Save kept the typed profile/skills; retry succeeded; refresh retained the saved skill; no JavaScript errors.
- Browser writes targeted only an isolated local fixture backed by an in-memory cloud stub. No live Supabase writes or email dispatches were made.

Sent dashboard Gmail import correction:

- Reproduced the reported 500 for Gmail-connected accounts: both `/sent` and `/api/gmail/sync-activity` raised `NameError: name 'gmail' is not defined`.
- Imported the existing Gmail service at module scope so both route handlers resolve it. No service behavior, schema, credentials or user data were changed.
- Added route regressions for Gmail-connected/SMTP accounts, tenant isolation, unauthenticated access and activity sync.
- Verification: **60 passed** in a credential-free copy across Sent, Gmail OAuth/activity, follow-up policy, KB Save, cloud payload fidelity and resume delivery. No live Gmail or Supabase writes were performed.
