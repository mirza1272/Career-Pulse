"""Phase 8 checks: bounded 87+ optimization loop, best-version kept, versions persisted."""
import inspect
import json
import os
import re
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from app.knowledge import load_candidate  # noqa: E402
from app.tailor import tailor_application_resume  # noqa: E402

JD = "We need a Python Backend Developer with FastAPI, Docker and SQL. Build REST APIs."
candidate = load_candidate()


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


# 1. Defaults: target 87, max 5 attempts (§U.1, FR-O-01).
sig = inspect.signature(tailor_application_resume)
check("default target_score is 87", sig.parameters["target_score"].default == 87.0,
      f"got {sig.parameters['target_score'].default}")
check("default max_attempts is 5", sig.parameters["max_attempts"].default == 5,
      f"got {sig.parameters['max_attempts'].default}")

# 2. The old bug: custom_focus (e.g. the Phase 5 optimizer brief) silently
#    disabled the loop. Now the loop must run with custom instructions locked.
res = tailor_application_resume(
    application_id="test-loop-8a",
    jd_text=JD,
    job_title="Backend Developer",
    custom_focus="Emphasize Python and FastAPI skills from the profile.",
    target_score=100.0,  # unreachable -> loop must iterate, not skip
    candidate=candidate,
)
check("loop runs despite custom_focus (old skip bug fixed)", res.ats_attempts > 1,
      f"attempts={res.ats_attempts}")
check("attempts bounded at 5", res.ats_attempts <= 5, f"attempts={res.ats_attempts}")
check("history length matches attempts", len(res.iterations) == res.ats_attempts,
      f"{len(res.iterations)} vs {res.ats_attempts}")
check("stop reason recorded", res.stop_reason in ("target_reached", "plateau", "max_iterations"),
      f"got {res.stop_reason!r}")

# 3. Best version always kept.
best_hist = max(h["score"] for h in res.iterations)
check("returned score == best iteration score", abs(res.ats_score - best_hist) < 0.01,
      f"score={res.ats_score} best_hist={best_hist}")

# 4. History entries carry deltas and gap-closure info.
h1 = res.iterations[0]
check("history has attempt/score/delta/gaps_closed/via",
      all(k in h1 for k in ("attempt", "score", "delta", "gaps_closed", "via", "note")))
if len(res.iterations) > 1:
    check("deltas are numeric", all(isinstance(h["delta"], (int, float)) for h in res.iterations[1:]))

# 5. KB snapshot hash present and well-formed.
check("kb_snapshot_hash is sha256 hex", bool(re.fullmatch(r"[0-9a-f]{64}", res.kb_snapshot_hash or "")),
      f"got {(res.kb_snapshot_hash or '')[:16]}...")

# 6. Normal run (no custom focus): bounded, terminates with a reason.
res2 = tailor_application_resume(
    application_id="test-loop-8b", jd_text=JD, job_title="Backend Developer", candidate=candidate,
)
check("plain run bounded", 1 <= res2.ats_attempts <= 5, f"attempts={res2.ats_attempts}")
check("plain run stop reason", res2.stop_reason in ("target_reached", "plateau", "max_iterations"))
check("target_reached implies >=87",
      (not res2.target_reached) or res2.ats_score >= 87.0, f"score={res2.ats_score}")

# 7. Version persistence: rows + versioned HTML files, restorable.
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import record_resume_version  # noqa: E402
from app.models import Application, Base, ResumeVersion  # noqa: E402
from app.tailor import RESUMES_OUTPUT_DIR  # noqa: E402

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
s = Session(engine)
app_row = Application(email="a@b.c", job_title="Backend Developer", jd_text=JD)
s.add(app_row)
s.flush()

fake = SimpleNamespace(
    resume_pdf_path="", resume_html_path="", ats_score=71.5, ats_attempts=3,
    variant="se_al", kb_snapshot_hash="ab" * 32,
    iterations=[{"attempt": 1, "score": 68.0}, {"attempt": 2, "score": 71.5}],
)
src = RESUMES_OUTPUT_DIR / "application_1.html"
RESUMES_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
src.write_text("<html>fake</html>", encoding="utf-8")
fake.resume_html_path = str(src)

v1 = record_resume_version(s, application_id=app_row.id, result=fake, jd_text=JD, job_title="Backend Developer")
check("version 1 recorded", v1 is not None and v1.version_no == 1)
check("versioned html file exists", v1 is not None and os.path.exists(v1.resume_path), v1.resume_path if v1 else "")
check("score history persisted", v1 is not None and json.loads(v1.score_history_json)[1]["score"] == 71.5)
check("kb hash persisted", v1 is not None and v1.kb_snapshot_hash == "ab" * 32)

v2 = record_resume_version(s, application_id=app_row.id, result=fake, jd_text=JD, job_title="Backend Developer")
check("version 2 increments", v2 is not None and v2.version_no == 2)
check("v1 file untouched by v2", os.path.exists(v1.resume_path) and v1.resume_path != v2.resume_path)
rows = s.query(ResumeVersion).filter_by(application_id=app_row.id).count()
check("two version rows", rows == 2, f"rows={rows}")
s.close()

print("ALL OPTIMIZATION LOOP CHECKS PASSED")
