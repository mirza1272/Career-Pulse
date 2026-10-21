"""Fit real content to a balanced page without oversized type or lost content."""
import re

import pytest
from bs4 import BeautifulSoup
from pypdf import PdfReader

from app.resume_builder import (
    PageOverflowError, _build_pdf_page, _render_pdf_fallback,
    _PDF_MARGIN_MM, PDF_RENDERER_VERSION,
)
from test_resume_delivery_regressions import resume_html


def geometry(page):
    positions = []
    page.extract_text(visitor_text=lambda text, cm, tm, font, size:
                      positions.append((text, tm[5] * 25.4 / 72, size)) if text.strip() else None)
    return positions


@pytest.mark.parametrize('template', [
    'apex_modern', 'oxford_editorial', 'silicon_compact', 'monarch_executive',
    'nova_minimalist', 'se_al', 'se_am', 'se_fd',
])
@pytest.mark.parametrize('bullets', [40, 55])
def test_moderate_and_dense_content_fill_the_page_with_readable_type(tmp_path, template, bullets):
    html = tmp_path / 'resume.html'
    html.write_text(resume_html(bullets).replace('data-template="apex_modern"', f'data-template="{template}"'))
    pdf = html.with_suffix('.pdf')
    _render_pdf_fallback(html, pdf, page_target=1)
    reader = PdfReader(pdf)
    assert len(reader.pages) == 1
    positions = geometry(reader.pages[0])
    assert _PDF_MARGIN_MM <= min(y for _, y, _ in positions) <= _PDF_MARGIN_MM + 8
    sizes = [size for text, _, size in positions if 'Detail' in text]
    assert min(sizes) >= 9 and max(sizes) <= 11.52 + 0.01
    text = reader.pages[0].extract_text()
    assert {int(i) for i in re.findall(r'Detail (\d+):', text)} == set(range(bullets))
    assert 'Portfolio' in text
    assert reader.metadata['/Creator'] == PDF_RENDERER_VERSION


def test_sparse_resume_uses_bounded_type_and_spacing(tmp_path):
    html = tmp_path / 'sparse.html'
    html.write_text(resume_html(8))
    pdf = html.with_suffix('.pdf')
    _render_pdf_fallback(html, pdf)
    page = PdfReader(pdf).pages[0]
    positions = geometry(page)
    assert max(size for _, _, size in positions) <= 28.8 + 0.01
    assert 'Detail 7:' in page.extract_text()
    # Very short content must not become giant text or widely separated lines.
    assert min(y for _, y, _ in positions) > 100


@pytest.mark.parametrize('flow', [False, True])
def test_all_four_layout_margins_are_equal(flow):
    pdf = _build_pdf_page(BeautifulSoup(resume_html(), 'html.parser'), flow_pages=flow)
    assert pdf.l_margin == pdf.r_margin == pdf.t_margin == pdf.b_margin == _PDF_MARGIN_MM


def test_long_resume_still_flows_and_one_page_limit_is_enforced(tmp_path):
    html = tmp_path / 'long.html'
    html.write_text(resume_html(110))
    pdf = html.with_suffix('.pdf')
    with pytest.raises(PageOverflowError):
        _render_pdf_fallback(html, pdf, page_target=1)
    _render_pdf_fallback(html, pdf)
    pages = PdfReader(pdf).pages
    assert len(pages) == 2
    text = '\n'.join(page.extract_text() for page in pages)
    assert {int(i) for i in re.findall(r'Detail (\d+):', text)} == set(range(110))
    for page in pages:
        assert min(y for _, y, _ in geometry(page)) >= _PDF_MARGIN_MM
