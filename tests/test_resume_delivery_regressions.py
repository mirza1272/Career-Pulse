"""Selected PDFs must survive preview, download, dispatch and cache recovery."""

import io
import json
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from fpdf import FPDF
from pypdf import PdfReader

import app.main as main
from app import supabase_storage
from app.auth import create_session_token, hash_password
from app.db import get_session
from app.models import Application, User
from app.outbound import OutboundResult
from app.resume_builder import (
    PDF_RENDERER_VERSION, TEMPLATES_DIR, PageOverflowError, _build_pdf_page, _render_pdf_fallback,
)


def pdf_bytes(text="Original uploaded resume"):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(0, 10, text)
    return bytes(pdf.output())


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "RESUMES_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "sync_app_to_supabase", lambda *a, **k: None)
    monkeypatch.setattr(main, "_maybe_hydrate_from_cloud", lambda *a: None)
    monkeypatch.setattr(main, "get_supabase_client", None)
    monkeypatch.setattr(supabase_storage, "download_resume_from_supabase", lambda *a: None)
    email = f"delivery-{uuid.uuid4().hex}@example.test"
    with get_session() as session:
        user = User(email=email, name="Delivery User", password_hash=hash_password("test-password"),
                    smtp_verified=True, smtp_username=email, smtp_password_encrypted="test-only",
                    knowledge_base_json=json.dumps({"name": "Delivery User", "email": email,
                                                    "summary": "Build reliable software.", "skills": ["Python"]}))
        session.add(user)
        session.flush()
        row = Application(user_id=user.id, email="hiring@example.test", job_title="Engineer",
                          company="Test Company", jd_text="Python", status="ready")
        session.add(row)
        session.commit()
        uid, aid = user.id, row.id
    client = TestClient(main.app)
    client.cookies.set("careerpulse_auth", create_session_token(uid, email))
    return client, aid, tmp_path


def select_pdf(aid, path):
    with get_session() as session:
        session.get(Application, aid).resume_path = str(path)


def test_download_returns_exact_selected_pdf_and_attachment_header(delivery):
    client, aid, root = delivery
    original = pdf_bytes()
    path = root / f"application_{aid}_custom.pdf"
    path.write_bytes(original)
    select_pdf(aid, path)
    for suffix, disposition in [("", "inline"), ("?download=1", "attachment")]:
        response = client.get(f"/application/{aid}/resume.pdf{suffix}")
        assert response.status_code == 200
        assert response.content == original
        assert response.headers["content-disposition"].startswith(disposition)
        assert response.headers["content-type"] == "application/pdf"


def test_email_mime_preserves_original_pdf_bytes(delivery):
    _, aid, root = delivery
    path = root / f"application_{aid}_custom.pdf"
    original = pdf_bytes()
    path.write_bytes(original)
    message = main.outbound_guard.compose_message(
        recipient="hiring@example.test", subject="Application", body="Resume attached.",
        resume_pdf_path=path,
    )
    from email import policy
    from email.parser import BytesParser
    decoded = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
    attachments = list(decoded.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[0].get_payload(decode=True) == original


def test_sending_keeps_uploaded_pdf_and_prevents_duplicate_send(delivery, monkeypatch):
    client, aid, root = delivery
    original = pdf_bytes()
    path = root / f"application_{aid}_custom.pdf"
    path.write_bytes(original)
    select_pdf(aid, path)
    # An existing generated resume must never replace the selected upload.
    (root / f"application_{aid}.html").write_text("<html>Different generated resume</html>")
    send = Mock(return_value=OutboundResult("sent", "hiring@example.test", "hiring@example.test"))
    monkeypatch.setattr(main.outbound_guard, "send", send)
    response = client.post(f"/application/{aid}/send", follow_redirects=False)
    assert response.status_code == 303
    assert Path(send.call_args.kwargs["resume_pdf_path"]).read_bytes() == original
    assert client.get(f"/application/{aid}/resume.pdf?download=1").content == original
    client.post(f"/application/{aid}/send", follow_redirects=False)
    assert send.call_count == 1


def test_missing_custom_pdf_is_not_silently_regenerated(delivery, monkeypatch):
    client, aid, root = delivery
    select_pdf(aid, root / f"application_{aid}_custom.pdf")
    response = client.get(f"/application/{aid}/resume.pdf?download=1")
    assert response.status_code == 404
    send = Mock()
    monkeypatch.setattr(main.outbound_guard, "send", send)
    client.post(f"/application/{aid}/send", follow_redirects=False)
    send.assert_not_called()


def test_page_count_cannot_replace_uploaded_pdf(delivery):
    client, aid, root = delivery
    original = pdf_bytes()
    path = root / f"application_{aid}_custom.pdf"
    path.write_bytes(original)
    select_pdf(aid, path)
    (root / f"application_{aid}.html").write_text(resume_html())
    response = client.post(f"/application/{aid}/pdf-pages", data={"pages": "2"})
    assert response.status_code == 409
    assert client.get(f"/application/{aid}/resume.pdf?download=1").content == original


def test_lost_pdf_is_recovered_byte_for_byte_from_storage(delivery, monkeypatch):
    client, aid, root = delivery
    original = pdf_bytes()
    name = f"application_{aid}_custom.pdf"
    select_pdf(aid, Path("/previous/server/cache") / name)
    fetch = Mock(return_value=original)
    monkeypatch.setattr(supabase_storage, "download_resume_from_supabase", fetch)
    response = client.get(f"/application/{aid}/resume.pdf?download=1")
    assert response.status_code == 200 and response.content == original
    fetch.assert_called_once_with(name)
    assert (root / name).read_bytes() == original


def test_failed_pdf_render_stops_send_and_honors_selected_pages(delivery, monkeypatch):
    client, aid, root = delivery
    (root / f"application_{aid}.html").write_text("<html>Saved edit</html>")
    render = Mock(side_effect=PageOverflowError("Content exceeds 1 page"))
    send = Mock()
    monkeypatch.setattr(main, "render_pdf_from_html", render)
    monkeypatch.setattr(main.outbound_guard, "send", send)
    response = client.get(f"/application/{aid}/resume.pdf")
    assert response.status_code == 422
    assert render.call_args.kwargs["page_target"] == 1
    client.post(f"/application/{aid}/send", follow_redirects=False)
    send.assert_not_called()


def test_download_requires_ownership_before_fetching(delivery, monkeypatch):
    client, aid, root = delivery
    with get_session() as session:
        session.get(Application, aid).user_id = -1
    fetch = Mock()
    monkeypatch.setattr(supabase_storage, "download_resume_from_supabase", fetch)
    assert client.get(f"/application/{aid}/resume.pdf?download=1").status_code == 403
    fetch.assert_not_called()


def test_legacy_html_pointer_is_rendered_instead_of_downloaded_as_pdf(delivery):
    client, aid, root = delivery
    html = root / f"application_{aid}.html"
    html.write_text(resume_html())
    select_pdf(aid, html)
    response = client.get(f"/application/{aid}/resume.pdf?download=1")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")
    assert "Test User" in PdfReader(io.BytesIO(response.content)).pages[0].extract_text()


def test_storage_download_only_accepts_pdf_bytes(monkeypatch):
    monkeypatch.setattr(supabase_storage, "_get_env_vars", lambda: ("https://storage.example.test", "test-only", "resumes"))
    response = Mock(status_code=200, content=pdf_bytes())
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get.return_value = response
    monkeypatch.setattr(supabase_storage.httpx, "Client", Mock(return_value=client))
    assert supabase_storage.download_resume_from_supabase("application_1.pdf") == response.content
    response.content = b"<html>Expired object</html>"
    assert supabase_storage.download_resume_from_supabase("application_1.pdf") is None
    assert supabase_storage.download_resume_from_supabase("../private.pdf") is None


def test_dashboard_has_no_followup_panel(delivery):
    client, _, _ = delivery
    response = client.get("/")
    assert response.status_code == 200
    assert "Follow-Up Attention Queue" not in response.text
    assert "Review Follow-Up" not in response.text
    assert "Recent Dispatch Activity" in response.text


def test_preview_fonts_are_served_and_use_http_paths(delivery):
    client, aid, root = delivery
    assert client.get("/assets/fonts/fonts.css").status_code == 200
    assert client.get("/assets/fonts/Nunito-Regular.ttf").status_code == 200
    response = client.get(f"/application/{aid}/resume.html")
    assert "file://" not in response.text
    assert "/assets/fonts/Nunito-Regular.ttf" in response.text
    preview = client.get("/templates/preview/apex_modern")
    assert "file://" not in preview.text
    assert "style-src 'self'" in preview.headers["content-security-policy"]


def test_editor_embeds_the_same_pdf_that_is_downloaded(delivery):
    client, aid, root = delivery
    html = root / f"application_{aid}.html"
    html.write_text((TEMPLATES_DIR / "apex_modern.html").read_text())
    _render_pdf_fallback(html, html.with_suffix(".pdf"))
    select_pdf(aid, html.with_suffix(".pdf"))
    response = client.get(f"/application/{aid}")
    assert response.status_code == 200
    iframe = BeautifulSoup(response.text, "html.parser").find("iframe", id="resumeIframe")
    assert iframe["src"].startswith(f"/application/{aid}/resume.pdf?")
    assert client.get(iframe["src"].split("#")[0]).content == client.get(f"/application/{aid}/resume.pdf?download=1").content


def test_generated_pdf_bytes_match_preview_download_and_email(delivery, monkeypatch):
    client, aid, root = delivery
    html = root / f"application_{aid}.html"
    html.write_text((TEMPLATES_DIR / "apex_modern.html").read_text())
    preview_bytes = client.get(f"/application/{aid}/resume.pdf").content
    assert client.get(f"/application/{aid}/resume.pdf?download=1").content == preview_bytes
    attachments = []

    def capture_dispatch(**kwargs):
        message = main.outbound_guard.compose_message(
            recipient=kwargs["intended_recipient"], subject=kwargs["subject"],
            body=kwargs["body"], resume_pdf_path=kwargs["resume_pdf_path"], user=kwargs["user"],
        )
        attachments.extend(part.get_payload(decode=True) for part in message.iter_attachments())
        return OutboundResult("drafted", kwargs["intended_recipient"], kwargs["intended_recipient"], reason="Offline verification")

    monkeypatch.setattr(main.outbound_guard, "send", capture_dispatch)
    response = client.post(f"/application/{aid}/send", follow_redirects=False)
    assert response.status_code == 303
    assert attachments == [preview_bytes]


def test_old_unsent_export_is_refreshed_but_sent_pdf_is_preserved(delivery):
    client, aid, root = delivery
    pdf = root / f"application_{aid}.pdf"
    html = pdf.with_suffix(".html")
    html.write_text((TEMPLATES_DIR / "apex_modern.html").read_text())
    original = pdf_bytes("Old export without icons")
    pdf.write_bytes(original)
    select_pdf(aid, pdf)
    response = client.get(f"/application/{aid}/resume.pdf")
    assert response.status_code == 200
    assert response.content != original
    assert PdfReader(io.BytesIO(response.content)).metadata["/Creator"] == PDF_RENDERER_VERSION
    with get_session() as session:
        session.get(Application, aid).status = "sent"
    pdf.write_bytes(original)
    assert client.get(f"/application/{aid}/resume.pdf").content == original


def test_header_and_project_svg_icons_are_vector_graphics_in_the_pdf():
    from pypdf.generic import ContentStream

    def render_and_count_curves(soup):
        pdf = _build_pdf_page(soup)
        reader = PdfReader(io.BytesIO(bytes(pdf.output())))
        content = ContentStream(reader.pages[0]["/Contents"], reader)
        return sum(operator == b"c" for _, operator in content.operations), reader

    html = (TEMPLATES_DIR / "apex_modern.html").read_text()
    all_icons, reader = render_and_count_curves(BeautifulSoup(html, "html.parser"))
    without_projects = BeautifulSoup(html, "html.parser")
    for svg in without_projects.select(".proj svg"):
        svg.decompose()
    header_only, _ = render_and_count_curves(without_projects)
    without_any = BeautifulSoup(html, "html.parser")
    for svg in without_any.find_all("svg"):
        svg.decompose()
    no_icons, _ = render_and_count_curves(without_any)
    assert all_icons > header_only > no_icons
    text = reader.pages[0].extract_text()
    assert "Portfolio" in text and "LinkedIn" in text
    links = [a.get_object().get("/A", {}).get("/URI", "") for a in reader.pages[0].get("/Annots", [])]
    assert any("linkedin" in link for link in links)


def test_inline_emphasis_does_not_join_words_in_export():
    soup = BeautifulSoup('<body><h2>Profile</h2><p>Built <strong>reliable</strong> services with <em>Python</em> safely.</p></body>', "html.parser")
    pdf = _build_pdf_page(soup)
    text = PdfReader(io.BytesIO(bytes(pdf.output()))).pages[0].extract_text()
    assert "Built reliable services with Python safely." in text


def test_nonbreaking_hyphen_is_not_dropped_from_export():
    soup = BeautifulSoup('<body><h2>Profile</h2><p>Built LLM\u2011powered and real\u2010world services.</p></body>', "html.parser")
    pdf = _build_pdf_page(soup)
    text = PdfReader(io.BytesIO(bytes(pdf.output()))).pages[0].extract_text()
    assert "LLM-powered" in text and "real-world" in text


def resume_html(bullets=8):
    content = "".join(f"<li>Detail {i}: Built reliable Python services and tested deployment pipelines.</li>"
                      for i in range(bullets))
    return f'''<html><body data-template="apex_modern"><span class="name">Test User</span>
    <span class="title">Software Engineer</span><div class="contact">
    <span class="item">long.engineering.email.address@example.test</span>
    <span class="item"><a href="https://linkedin.com/in/user">LinkedIn</a></span>
    <span class="item"><a href="https://github.com/user">GitHub</a></span>
    <span class="item"><a href="https://portfolio.example.test">Portfolio</a></span>
    </div><h2>Experience</h2><ul>{content}</ul></body></html>'''


@pytest.mark.parametrize("template", ["apex_modern", "oxford_editorial", "silicon_compact", "monarch_executive", "nova_minimalist"])
def test_pdf_has_distinct_embedded_font_names_and_whole_contact_labels(template):
    soup = BeautifulSoup(resume_html().replace('data-template="apex_modern"', f'data-template="{template}"'), "html.parser")
    pdf = _build_pdf_page(soup, scale_factor=1.28)
    reader = PdfReader(io.BytesIO(bytes(pdf.output())))
    fonts = [f.get_object() for f in reader.pages[0]["/Resources"]["/Font"].values()]
    names = [f["/BaseFont"] for f in fonts]
    assert len(names) == len(set(names))
    assert all(f["/Subtype"] == "/Type0" for f in fonts)
    assert reader.trailer["/Root"]["/OpenAction"][1] == "/Fit"
    fragments = []
    reader.pages[0].extract_text(visitor_text=lambda text, *args: fragments.append(text))
    assert any("Portfolio" in text for text in fragments)
    assert "TEST USER" in reader.pages[0].extract_text().upper()


def test_font_and_spacing_adapt_without_tiny_text_or_missing_content(tmp_path):
    fonts = []
    for count in [8, 55]:
        html = tmp_path / f"resume-{count}.html"
        pdf = html.with_suffix(".pdf")
        html.write_text(resume_html(count))
        _render_pdf_fallback(html, pdf)
        reader = PdfReader(pdf)
        text = "".join(page.extract_text() for page in reader.pages)
        assert f"Detail {count - 1}:" in text
        sizes = []
        for page in reader.pages:
            page.extract_text(visitor_text=lambda text, cm, tm, font, size: sizes.append(size) if "Detail" in text else None)
        assert min(sizes) >= 9
        fonts.append(max(sizes))
        assert len(reader.pages) <= 2
    assert fonts[0] > fonts[1]
