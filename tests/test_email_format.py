"""Phase 13 checks (§U.11): application email = greeting + 3 paragraphs +
valediction + LinkedIn. No live LLM needed: we capture the prompt sent to the
LLM (mocked _post) and check the §U.11 structure rules are in it, then verify
the deterministic fallback email itself conforms.

Run with:  AUTH_PASSWORD=test123456 python tests/test_email_format.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import app.llm as llm  # noqa: E402
from app.knowledge import Candidate  # noqa: E402


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({detail})" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


CAND = Candidate(
    name="Test User",
    email="test@example.com",
    phone="+92 300 0000000",
    links={"linkedin": "https://www.linkedin.com/in/test-user"},
    skills=["Python", "FastAPI", "LLMs"],
    projects=[{"short_name": "Nemetron", "bullet": "RAG platform"}],
)

# 1. The prompt sent to the LLM must encode the §U.11 structure.
captured = {}


def fake_post(payload):
    captured["system"] = payload["messages"][0]["content"]
    captured["user"] = payload["messages"][1]["content"]
    return None  # force the deterministic fallback below


real_post = llm._post
llm._post = fake_post
try:
    email = llm.write_application_email(
        CAND, jd_text="We need a Python developer. Contact Sarah Ahmed for details.",
        company="Acme", role="AI Engineer",
    )
finally:
    llm._post = real_post

system = captured.get("system", "")
check("prompt requires greeting 'Dear Hiring Manager,'", "Dear Hiring Manager," in system)
check("prompt allows 'Dear [Name],' when JD names a contact", "Dear [Name]," in system)
check("prompt requires exactly 3 paragraphs", "exactly 3 paragraphs" in system)
check("prompt requires 'Best regards,' valediction", "Best regards," in system)
check("prompt requires the LinkedIn URL in the sign-off", "LinkedIn" in system)
check("prompt forbids inventing a greeting name", "Never invent a name" in system)

user_msg = captured.get("user", "")
check("user message passes the LinkedIn URL to the model",
      "https://www.linkedin.com/in/test-user" in user_msg, user_msg[:200])

# 2. The deterministic fallback email itself must conform to §U.11.
check("email opens with a greeting", email.startswith("Dear "), email[:40])
check("email has 'Best regards,' valediction", "Best regards," in email)

blocks = [b.strip() for b in email.split("\n\n") if b.strip()]
check("email = greeting + 3 paragraphs + sign-off sentence + Best regards block",
      len(blocks) == 6, f"{len(blocks)} blocks")
if len(blocks) == 6:
    check("block 1 is the greeting", blocks[0].startswith("Dear "), blocks[0][:40])
    check("block 5 is the sign-off sentence",
          blocks[4] == "I have attached my resume for your review. Thank you for your time and consideration.",
          blocks[4][:60])
    sig_lines = blocks[5].splitlines()
    check("block 6 = Best regards + name/email/phone/LinkedIn",
          sig_lines[0].strip() == "Best regards,"
          and any("Test User" in l for l in sig_lines)
          and any("test@example.com" in l for l in sig_lines)
          and any("linkedin.com" in l for l in sig_lines),
          blocks[5][:120])
check("LinkedIn URL present", "https://www.linkedin.com/in/test-user" in email)
check("no markdown in email", "**" not in email and "#" not in email.split("\n")[0])

# 3. Fallback still works when LinkedIn is missing (no dangling empty line).
cand_nolink = Candidate(name="No Link", email="n@x.com", phone="123",
                        links={}, skills=["Python"])
email2 = llm._deterministic_email(cand_nolink, "Acme", "AI Engineer", None)
check("fallback valid without LinkedIn",
      email2.startswith("Dear ") and "Best regards," in email2
      and "No Link" in email2)
check("no empty signature lines without LinkedIn",
      all(l.strip() for l in email2.splitlines()[-4:]), email2.splitlines()[-4:])

print("ALL EMAIL FORMAT CHECKS PASSED")
