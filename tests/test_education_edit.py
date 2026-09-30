from app.resume_builder import rebuild_resume_from_custom_edits, extract_regions, TEMPLATES_DIR
from app.knowledge import load_candidate

def test_structured_education_form_fields():
    candidate = load_candidate()
    base_html = (TEMPLATES_DIR / "se_al.html").read_text(encoding="utf-8")
    
    # Simulate custom education structured inputs
    edu_degree = "Master of Science in Artificial Intelligence"
    edu_institution = "National University of Sciences & Technology (NUST)"
    edu_dates = "2024 – 2026"
    edu_location = "Islamabad, Pakistan"
    
    import html as _html
    deg_label = f"{edu_degree},"
    line1 = f'  <div class="entry">\n    <div class="left"><strong>{_html.escape(deg_label)}</strong></div>\n    <div class="right">{_html.escape(edu_dates)}</div>\n  </div>'
    line2 = f'  <div class="entry" style="margin-top:0;">\n    <div class="left"><em>{_html.escape(edu_institution)}</em></div>\n    <div class="sub-right">{_html.escape(edu_location)}</div>\n  </div>'
    education_html = f"<h2>Education</h2>\n{line1}\n{line2}"
    
    built = rebuild_resume_from_custom_edits(
        base_html=base_html,
        education_html=education_html,
        candidate=candidate,
        job_title="AI Engineer",
        jd_text="AI job description",
    )
    
    regions = extract_regions(built.html_content)
    assert "Master of Science in Artificial Intelligence" in regions.get("EDUCATION", "")
    assert "National University of Sciences &amp; Technology" in regions.get("EDUCATION", "")
    assert "2024 – 2026" in regions.get("EDUCATION", "")
    assert "Islamabad, Pakistan" in regions.get("EDUCATION", "")
