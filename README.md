# Career Pulse & Radar 🚀

> **Autonomous AI Job Discovery, OCR Intake, Executive Resume Tailoring & One-Click Email Application Engine**

Career Pulse is an end-to-end intelligent career automation platform. It accepts job postings via text or clipboard screenshot paste (`Ctrl + V`), extracts structured details via Groq AI (`openai/gpt-oss-120b`), generates tailored professional resumes with an ATS optimization loop, and dispatches customized application emails upon human approval.

---

## ✨ Key Features

- **📸 2-Box Simplified Intake (`/new`)**:
  - **Box 1 (Screenshot / Image Dropzone)**: Drag & drop, browse, or press `Ctrl + V` to paste screenshots directly from your clipboard.
  - **Box 2 (Raw JD Text)**: Paste unstructured job descriptions directly.
  - **Single Action Button**: `✨ Process with AI & Create Application`.
- **🤖 Groq LLM & RapidOCR Extraction**:
  - High-speed local RapidOCR extracts text from job screenshots without external binaries.
  - Groq AI (`openai/gpt-oss-120b`, with 3-key rotation + llama fallbacks) extracts role title, company, recruiter email, and requirements.
- **📄 Precision Resume Generator**:
  - Tailored 1-page / 2-page resumes with an ATS optimization loop (target 87+, bounded at 5 iterations, plateau stop).
  - ATS breakdown is fully traceable: parsing safety (0–40) + job relevance (0–60).
  - Resume editor with manual-edit locks that survive regeneration and version restores.
  - Version history — every generation is restorable, and restores are undoable.
- **📧 Outbound Email Dispatcher**:
  - Guarded SMTP dispatch (supports Gmail App Passwords).
  - Professional subject lines (`Application for {role} — {company}`).
  - **Explicit approval workflow**: `draft → ready → pending_approval → approved → sent`. Nothing sends without human approval, ever. Content edits reset approval to `ready`.
- **📡 Radar Discovery Engine** (separate module):
  - Crawls 72+ top Pakistani tech companies across Lahore, Islamabad, Faisalabad, and Remote.
  - Pushes fresh jobs (≤ 3 days old) into Career Pulse via the `/api/applications` API.

---

## 🛠️ Project Structure

```
├── app/
│   ├── main.py             # FastAPI web application routes
│   ├── intake.py           # RapidOCR + Groq LLM structured parser
│   ├── resume_builder.py   # Modern resume generator & multi-template engine
│   ├── tailor.py           # ATS optimization loop
│   ├── outbound.py         # SMTP email dispatcher with safety guards
│   ├── ats.py              # ATS scoring (parsing safety + job relevance)
│   ├── matching.py         # JD ↔ Knowledge Base gap analysis
│   ├── knowledge.py        # Candidate profile / Knowledge Base
│   └── supabase_storage.py # Supabase cloud storage integration
├── radar/                  # Job discovery engine (Project 1)
├── config/candidate/       # Candidate profile YAML & project portfolio
├── templates/              # Jinja2 HTML templates & modern resume layouts
├── tests/                  # Pytest suite
├── requirements.txt        # Python dependencies
├── selfcheck.py            # Environment self-check
├── schema.sql              # Supabase SQL migrations
└── .env                    # Credentials (NEVER commit to GitHub)
```

---

## Quickstart (Local)

### 1. Install dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment
Copy `.env.example` to `.env` and fill in:
- `GROQ_API_KEY_1`, `GROQ_API_KEY_2`, `GROQ_API_KEY_3` — Groq API keys (required)
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` — Supabase (required for cloud persistence; app falls back to in-memory SQLite without it)
- `ADMIN_EMAIL`, `PASSWORD` — login credentials
- `SMTP_*` — email dispatch settings
- `TEST_MODE=1`, `ALLOW_REAL_EMAIL=0` — keep these for safe local testing (no real emails sent)

### 3. Start the server
```bash
# If outbound calls fail in a sandboxed/proxied environment:
export NO_PROXY="localhost,127.0.0.1" no_proxy="localhost,127.0.0.1"
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8770
```
Open **http://127.0.0.1:8770** and log in with your `ADMIN_EMAIL` / `PASSWORD`.

### 4. Run the test suite
```bash
.venv/bin/python -m pytest tests/ -q
```

---

## ⚠️ Notes

- **Persistence**: with a valid Supabase service-role key, applications sync to the cloud. Without it, the app runs on in-memory SQLite and data is lost on restart (by design, graceful degradation).
- **Test mode**: `TEST_MODE=1` + `ALLOW_REAL_EMAIL=0` means emails are never really dispatched. The approval workflow (`draft → ready → pending_approval → approved → sent`) is still fully enforced.
- **Secrets**: `.env` is gitignored. Never commit it, never hardcode credentials in source.

---

## 📜 License
MIT License. Developed for autonomous career workflows.
