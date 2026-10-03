"""Physical PDF pagination regressions; these tests run the real XeLaTeX engine."""

import json
import shutil

import pytest

from texopt import cli


SOURCE = r"""\documentclass{article}
\usepackage[a4paper,margin=16mm]{geometry}
\begin{document}
Company header\par
\title{Record {A}}
\date{}
\maketitle
First page body.
% LEXOID_PAGE_COMPLETED: 1/2
\newpage
Second page body.
% LEXOID_PAGE_COMPLETED: 2/2
\end{document}
"""


def evidence():
    return {"schema": "recognition/v1", "pages": [
        {"page": 1, "render": {"dpi": 72, "width": 842, "height": 595, "rotation": 90}},
        {"page": 2, "render": {"dpi": 72, "width": 595, "height": 842, "rotation": 0}},
    ]}


def test_title_and_mixed_orientation_have_one_output_page_per_source(tmp_path):
    from .page_layout import prepare_layout

    if not shutil.which("xelatex"):
        pytest.skip("XeLaTeX is required")
    original = tmp_path / "original.tex"
    original.write_text(SOURCE)
    ok, log = cli._compile_latex(original, tmp_path, "xelatex", 60)
    assert ok and "(3 pages)" in log

    source, report = prepare_layout(SOURCE, evidence())
    target = tmp_path / "fixed.tex"
    target.write_text(source)
    ok, log = cli._compile_latex(target, tmp_path, "xelatex", 60, layout_report=report)
    assert ok, log
    assert report["ok"] is True
    assert report["actual_pages"] == 2
    assert report["page_map"] == [
        {"source_page": 1, "start": 1, "end": 1},
        {"source_page": 2, "start": 2, "end": 2},
    ]
    assert report["actual_sizes"] == pytest.approx([(842, 595), (595, 842)], abs=1)
    assert target.with_suffix(".layout.pdf").is_file()


def test_comment_only_source_page_still_produces_one_pdf_page(tmp_path):
    from .page_layout import prepare_layout

    if not shutil.which("xelatex"):
        pytest.skip("XeLaTeX is required")
    source = SOURCE.replace("First page body.", "% Visually blank source page")
    source = source.replace("Second page body.", "% Another visually blank source page")
    fixed, report = prepare_layout(source, evidence())
    target = tmp_path / "blank-pages.tex"
    target.write_text(fixed)
    ok, log = cli._compile_latex(target, tmp_path, "xelatex", 60, layout_report=report)
    assert ok, log
    assert report["actual_pages"] == 2
    assert report["page_map"] == [
        {"source_page": 1, "start": 1, "end": 1},
        {"source_page": 2, "start": 2, "end": 2},
    ]


def test_extra_break_is_reported_but_does_not_fail_compilation(tmp_path):
    from .page_layout import prepare_layout

    if not shutil.which("xelatex"):
        pytest.skip("XeLaTeX is required")
    src = SOURCE.replace("First page body.", "First page body.\\newpage\nUnexpected spill.")
    source, report = prepare_layout(src, evidence())
    target = tmp_path / "overflow.tex"
    target.write_text(source)
    ok, log = cli._compile_latex(target, tmp_path, "xelatex", 60, layout_report=report)
    assert ok
    assert report["actual_pages"] == 3
    assert report["page_map"][0] == {"source_page": 1, "start": 1, "end": 2}
    assert report["ok"] is False
    assert report["errors"]


def test_text_outside_paper_fails_export_check_even_when_tex_compiles(tmp_path):
    from .page_layout import prepare_layout

    src = SOURCE.replace('First page body.',
        r'\noindent\hspace*{\paperwidth}CLIPPED TEXT\par')
    fixed, report = prepare_layout(src, evidence())
    target = tmp_path / 'clipped.tex'
    target.write_text(fixed)
    ok, log = cli._compile_latex(target, tmp_path, 'xelatex', 60, layout_report=report)
    assert 'exit 0' in log
    assert report['outside_pages']
    assert not ok


def test_retry_targets_clipped_source_page_after_allowed_extra_pages(tmp_path, monkeypatch):
    source = tmp_path / 'source.tex'
    source.write_text(SOURCE)
    metadata = tmp_path / 'evidence.json'
    metadata.write_text(json.dumps(evidence()))
    attempted = []

    def compile_result(*args, layout_report, **kwargs):
        attempted.append(dict(layout_report['profiles']))
        layout_report.update(page_map=[
            {'source_page': 1, 'start': 1, 'end': 2},
            {'source_page': 2, 'start': 3, 'end': 3},
        ], outside_pages=[3] if len(attempted) == 1 else [])
        return len(attempted) > 1, 'synthetic compiler result'

    monkeypatch.setattr(cli, '_compile_latex', compile_result)
    assert cli.main(['optimise', str(source), '-o', str(tmp_path / 'result.tex'),
                     '--no-llm', '--page-layout-evidence', str(metadata)]) == 0
    assert attempted == [{'1': 0, '2': 0}, {'1': 0, '2': 1}]


@pytest.mark.parametrize('environment', ['tabular', 'minipage'])
@pytest.mark.parametrize('declarations', ['', r'\normalsize', r'\small\normalfont\bfseries'])
def test_noindent_signature_after_panel_starts_a_new_paragraph(tmp_path, environment, declarations):
    import pypdfium2 as pdfium
    from .page_layout import prepare_layout

    panel = (r'\begin{tabular}{p{0.94\linewidth}}Form\\\end{tabular}'
             if environment == 'tabular' else
             r'\begin{minipage}{0.96\linewidth}Form\end{minipage}')
    body = ('\\noindent' + panel + '\n' + declarations + '\n% #VALUE_ID: SIGNATURE\n'
            r'\noindent\hspace{0.40\linewidth}SIGNATURE Alice 2024.9.26')
    source, report = prepare_layout(SOURCE.replace('First page body.', body), evidence())
    target = tmp_path / 'signature.tex'
    target.write_text(source)
    ok, log = cli._compile_latex(target, tmp_path, 'xelatex', 60, layout_report=report)
    assert ok, log
    assert not report['outside_pages']
    doc = pdfium.PdfDocument(str(target.with_suffix('.layout.pdf')))
    text = doc[0].get_textpage().get_text_bounded()
    assert 'SIGNATURE Alice 2024.9.26' in text
    doc.close()


def test_long_url_inside_field_macros_wraps_without_changing_characters(tmp_path):
    import pypdfium2 as pdfium
    from .page_layout import prepare_layout

    url = 'https://static.dingtalk.com/media/' + 'aB35' * 30 + '_960_1280.jpg?q=1&n=%20$2#image'
    escaped = url
    for char in '_&#%$':
        escaped = escaped.replace(char, '\\' + char)
    body = (r'\noindent\begin{tabular}{p{0.95\linewidth}}'
            r'\hwfield{ID}{\fieldvalue{\texttt{' + escaped + r'}}}\\\end{tabular}')
    original = SOURCE.replace(r'\begin{document}',
        '\\newcommand{\\hwfield}[2]{#2}\n\\newcommand{\\fieldvalue}[1]{#1}\n\\begin{document}')
    source, report = prepare_layout(original.replace('First page body.', body), evidence())
    target = tmp_path / 'url.tex'
    target.write_text(source)
    ok, log = cli._compile_latex(target, tmp_path, 'xelatex', 60, layout_report=report)
    assert ok, log
    assert not report['outside_pages']
    doc = pdfium.PdfDocument(str(target.with_suffix('.layout.pdf')))
    text = ''.join(doc[0].get_textpage().get_text_bounded().split())
    assert url in text
    doc.close()


def test_plain_text_after_panel_on_next_line_starts_below_panel(tmp_path):
    import pypdfium2 as pdfium
    from .page_layout import prepare_layout

    body = (r'\noindent\begin{tabular}{p{0.60\linewidth}}FORM\end{tabular}'
            '\n% #VALUE_ID: SIGNATURE\nSIGNATURE Alice 2024.9.26')
    source, report = prepare_layout(SOURCE.replace('First page body.', body), evidence())
    target = tmp_path / 'plain-signature.tex'
    target.write_text(source)
    ok, log = cli._compile_latex(target, tmp_path, 'xelatex', 60, layout_report=report)
    assert ok, log
    doc = pdfium.PdfDocument(str(target.with_suffix('.layout.pdf')))
    page = doc[0].get_textpage()
    text = page.get_text_range(force_this=True)
    form = page.get_charbox(text.index('FORM'))
    signature = page.get_charbox(text.index('SIGNATURE'))
    assert signature[3] < form[1]
    page.close()
    doc.close()


@pytest.mark.parametrize('kind', ['negative_indent', 'nested_wide_table'])
def test_form_content_stays_inside_its_available_width(tmp_path, kind):
    from .page_layout import prepare_layout

    if kind == 'negative_indent':
        body = '\\noindent\\hspace*{-1.6cm}LEFT LABEL\\\\\n\\hspace*{-1.6cm}SECOND LABEL'
    else:
        panel = (r'\begin{minipage}[t]{.32\linewidth}\centering'
                 r'\begin{tabular}{|p{.4\linewidth}|p{.4\linewidth}|}A&B\\'
                 r'\multicolumn{2}{|l|}{A VERY LONG UNBREAKABLE PRODUCT DESCRIPTION AND ADDRESS}\\'
                 r'\end{tabular}\end{minipage}')
        body = r'\noindent' + r'\hfill'.join([panel] * 3)
    source, report = prepare_layout(SOURCE.replace('First page body.', body), evidence())
    target = tmp_path / 'width.tex'
    target.write_text(source)
    ok, log = cli._compile_latex(target, tmp_path, 'xelatex', 60, layout_report=report)
    assert ok, log
    assert not report['outside_pages']


def test_panel_and_url_repairs_preserve_literals_and_are_repeatable():
    from .page_layout import _separate_panel_paragraphs, _wrap_text_urls

    literal = (r'\verb|\texttt{https://example.org/a\_b}|' + '\n'
               r'% \end{tabular}\normalsize\noindent' + '\n'
               r'\end{minipage}\hfill\begin{minipage}{.4\linewidth}Right')
    assert _wrap_text_urls(literal) == literal
    assert _separate_panel_paragraphs(literal) == literal
    inline = r'\end{tabular} SIGNATURE Alice'
    assert _separate_panel_paragraphs(inline) == inline
    plain = '\\end{tabular}\n% signature\nSIGNATURE Alice'
    fixed_plain = _separate_panel_paragraphs(plain)
    assert r'\par{}SIGNATURE' in fixed_plain
    assert _separate_panel_paragraphs(fixed_plain) == fixed_plain
    original = (r'\end{tabular}\normalsize' + '\n% field\n'
                r'\noindent\texttt{https://example.org/a\_b}')
    fixed = _wrap_text_urls(_separate_panel_paragraphs(original))
    assert r'\par\noindent' in fixed
    assert r'\LexoidTextUrl{https://example.org/a\_b}' in fixed
    assert _wrap_text_urls(_separate_panel_paragraphs(fixed)) == fixed


def test_landscape_overflow_keeps_size_until_next_source_page(tmp_path):
    from .page_layout import prepare_layout

    if not shutil.which("xelatex"):
        pytest.skip("XeLaTeX is required")
    src = SOURCE.replace("First page body.",
        r"First page body.\newpage\noindent\makebox[\linewidth][r]{RIGHT EDGE}")
    source, report = prepare_layout(src, evidence())
    target = tmp_path / "landscape-spill.tex"
    target.write_text(source)
    ok, log = cli._compile_latex(target, tmp_path, "xelatex", 60, layout_report=report)
    assert ok, log
    assert report["actual_sizes"] == pytest.approx([(842, 595), (842, 595), (595, 842)], abs=1)
    assert report["outside_pages"] == []


@pytest.mark.parametrize("width,height", [(842, 595), (595, 842)])
def test_margin_annotations_remain_visible_with_narrow_page_margins(tmp_path, width, height):
    import pypdfium2 as pdfium
    from .page_layout import prepare_layout

    note = 'Signature Alice Smith 2025.12.22 '
    src = SOURCE.replace('First page body.',
        r'\noindent\begin{minipage}{0.46\linewidth}Left panel\end{minipage}\hfill'
        r'\begin{minipage}{0.48\linewidth}Right panel\end{minipage}'
        r'\marginpar[Left alternative]{' + note * 8 + '}')
    data = evidence()
    data['pages'][0]['render'].update(width=width, height=height)
    fixed, report = prepare_layout(src, data)
    target = tmp_path / 'margin-note.tex'
    target.write_text(fixed)
    ok, log = cli._compile_latex(target, tmp_path, 'xelatex', 60, layout_report=report)
    assert ok, log
    assert report['outside_pages'] == []
    doc = pdfium.PdfDocument(str(target.with_suffix('.layout.pdf')))
    try:
        text = ''
        for page in doc:
            textpage = page.get_textpage()
            text += textpage.get_text_bounded()
            textpage.close()
            page.close()
        assert text.count('2025.12.22') == 8
        assert 'Left panel' in text and 'Right panel' in text
        assert 'Left alternative' not in text
    finally:
        doc.close()


@pytest.mark.parametrize("header", ["", r"\section*{Process log}\noindent Exported: 2026-09-07\par\vspace{20pt}"])
def test_tall_two_column_log_has_no_blank_or_clipped_page(tmp_path, header):
    import pypdfium2 as pdfium
    from .page_layout import prepare_layout

    if not shutil.which("xelatex"):
        pytest.skip("XeLaTeX is required")
    rows = "\n".join(rf"18:{i:02}:00 & Event {i}\\" for i in range(40))
    column = (r"\begin{minipage}[t]{0.47\linewidth}"
              r"\begin{tabular}{@{}ll@{}}" + rows +
              r"\end{tabular}\end{minipage}")
    body = header + r"\renewcommand{\arraystretch}{1.2}\noindent" + column + r"\hfill" + column + "\n\n\\hfill 5/8\n"
    src = SOURCE.replace("First page body.", body).replace(
        "Company header\\par\n\\title{Record {A}}\n\\date{}\n\\maketitle\n", "")
    source, report = prepare_layout(src, evidence())
    target = tmp_path / "two-column-log.tex"
    target.write_text(source)
    ok, log = cli._compile_latex(target, tmp_path, "xelatex", 60, layout_report=report)
    assert ok, log
    assert report["actual_pages"] == 2
    assert report["outside_pages"] == []
    doc = pdfium.PdfDocument(str(target.with_suffix(".layout.pdf")))
    try:
        page = doc[0]
        textpage = page.get_textpage()
        text = textpage.get_text_bounded()
        for i in range(40):
            assert text.count(f"18:{i:02}:00") == 2
        assert "5/8" in text
        textpage.close()
        page.close()
    finally:
        doc.close()


@pytest.mark.parametrize("author", ["Alice", r"Alice \and Bob"])
def test_layout_preserves_explicit_preamble_title_authors_and_date(tmp_path, author):
    import pypdfium2 as pdfium
    from .page_layout import prepare_layout

    src = SOURCE.replace(r"\begin{document}",
        "\\title{Record {A}}\n\\author{" + author + "}\n\\date{2026-09-05}\n\\begin{document}")
    src = src.replace("\\title{Record {A}}\n\\date{}\n", "")
    fixed, report = prepare_layout(src, evidence())
    target = tmp_path / "metadata.tex"
    target.write_text(fixed)
    ok, log = cli._compile_latex(target, tmp_path, "xelatex", 60, layout_report=report)
    assert ok, log
    document = pdfium.PdfDocument(str(target.with_suffix(".layout.pdf")))
    try:
        page = document[0]
        textpage = page.get_textpage()
        text = textpage.get_text_bounded()
        assert "Record A" in text and "Alice" in text and "2026-09-05" in text
        if "Bob" in author:
            assert "Bob" in text
        textpage.close()
        page.close()
    finally:
        document.close()


@pytest.mark.parametrize("kind", ["tall_table", "raised_spacer", "fixed_height", "rotated_panel", "unequal_baselines", "resized_table", "resized_star_table"])
def test_unbreakable_content_keeps_every_line_visible(tmp_path, kind):
    import pypdfium2 as pdfium
    from .page_layout import prepare_layout

    rows = "\n".join(rf"LINE{i:03d}\\" for i in range(45))
    if kind == "tall_table":
        body = r"\noindent\begin{tabular}{|p{0.9\linewidth}|}\hline " + rows + r"\hline\end{tabular}"
    elif kind in {"resized_table", "resized_star_table"}:
        star = '*' if kind == 'resized_star_table' else ''
        body = (r'\noindent\resizebox' + star + r'{\linewidth}{!}{\begin{tabular}{l}'
                + rows + r'\end{tabular}}')
    elif kind == "raised_spacer":
        body = (r"\noindent\begin{tabular}{|p{0.5\linewidth}|p{0.4\linewidth}|}\hline "
                r"\rule{0pt}{0.84\textheight} & \begin{minipage}[t]{\linewidth}" + rows +
                r"\end{minipage}\\\hline\end{tabular}")
    elif kind == "fixed_height":
        body = (r"\noindent\begin{minipage}[t]{0.9\linewidth}"
                r"\fbox{\begin{minipage}[t][40pt][t]{0.9\linewidth}" + rows +
                r"\end{minipage}}\end{minipage}")
    elif kind == "unequal_baselines":
        def panel(start, end):
            lines = "\n".join(rf"LINE{i:03d}\\" for i in range(start, end))
            return (r"\fbox{\begin{minipage}[t][40pt][t]{0.9\linewidth}TITLE\par"
                    r"\begin{tabular}{l}" + lines + r"\end{tabular}\par BOTTOM\end{minipage}}")
        body = (r"\noindent\begin{minipage}[t]{0.43\linewidth}"
                r"\begin{tabular}{l}" + rows[:rows.find("LINE025")] +
                r"\end{tabular}\end{minipage}\hfill"
                r"\begin{minipage}[t]{0.53\linewidth}" + panel(0, 22) +
                r"\par\vspace{8pt}" + panel(22, 45) + r"\end{minipage}")
    else:
        body = (r"\begin{center}\rotatebox{90}{\begin{minipage}{650pt}" + rows +
                r"\end{minipage}}\end{center}")
    source = SOURCE.replace("First page body.", body + r"\par FOOTER")
    fixed, report = prepare_layout(source, evidence())
    target = tmp_path / "fitted.tex"
    target.write_text(fixed)
    ok, log = cli._compile_latex(target, tmp_path, "xelatex", 60, layout_report=report)
    assert ok, log
    assert report["outside_pages"] == [], report
    assert report["actual_pages"] == 2
    doc = pdfium.PdfDocument(str(target.with_suffix(".layout.pdf")))
    try:
        page = doc[0]
        textpage = page.get_textpage()
        text = textpage.get_text_bounded()
        for i in range(45):
            assert f"LINE{i:03d}" in text
        assert "FOOTER" in text
        textpage.close()
        page.close()
    finally:
        doc.close()
    assert prepare_layout(fixed, evidence())[0] == fixed


def test_circled_numbers_and_bullets_have_visible_glyphs(tmp_path):
    import pypdfium2 as pdfium
    from .page_layout import prepare_layout

    symbols = "".join(chr(code) for code in range(0x2460, 0x246A)) + chr(0x25CF)
    original = SOURCE.replace(r"\documentclass{article}",
                              r"\documentclass[fontset=fandol]{ctexart}")
    source, report = prepare_layout(original.replace("First page body.", symbols), evidence())
    target = tmp_path / "symbols.tex"
    target.write_text(source)
    ok, log = cli._compile_latex(target, tmp_path, "xelatex", 60, layout_report=report)
    assert ok, log
    assert "Missing character:" not in log
    doc = pdfium.PdfDocument(str(target.with_suffix(".layout.pdf")))
    try:
        page = doc[0]
        textpage = page.get_textpage()
        text = textpage.get_text_bounded()
        assert all(symbol in text for symbol in symbols)
        textpage.close()
        page.close()
    finally:
        doc.close()


def test_same_total_with_wrong_page_assignment_is_rejected():
    from .page_layout import validate_page_map

    errors = validate_page_map(2, [(1, "start", 1), (1, "end", 2),
                                   (2, "start", 2), (2, "end", 2)], 2)
    assert errors


def test_spacer_repair_preserves_indented_rules_comments_and_braces():
    from .page_layout import _lower_empty_cell_spacers
    prefix = "\\begin{tabular}{ll}\n  \\hline\n% blank cell\n   "
    suffix = " & value\\\\\n\\end{tabular}"
    original = prefix + r"\rule{0pt}{8mm}" + suffix
    fixed = _lower_empty_cell_spacers(original)
    assert fixed == prefix + r"\rule[-\dimexpr8mm-\ht\strutbox\relax]{0pt}{8mm}" + suffix


def test_dimension_check_covers_spill_pages_when_page_count_differs(tmp_path):
    import pypdfium2 as pdfium
    from .page_layout import inspect_layout

    pdf = tmp_path / "wrong-spill.pdf"
    doc = pdfium.PdfDocument.new()
    try:
        for width, height in [(842, 595), (595, 842), (595, 842)]:
            page = doc.new_page(width, height)
            page.close()
        doc.save(str(pdf))
    finally:
        doc.close()
    pdf.with_suffix(".lxp").write_text("1,start,1\n1,end,2\n2,start,3\n2,end,3\n")
    report = {"expected_pages": 2, "expected_sizes": [(842, 595), (595, 842)]}
    inspect_layout(pdf, report)
    assert "Output page dimensions differ from source reading orientation" in report["errors"]


def test_layout_preserves_field_bytes_and_is_repeatable():
    from .page_layout import prepare_layout

    field = "% #VALUE_ID: LEX-P0001-V0001\n% #HANDWRITTEN: A\n\\fieldvalue{A}"
    source = SOURCE.replace("First page body.", field)
    first, _ = prepare_layout(source, evidence())
    second, _ = prepare_layout(first, evidence())
    assert field in first
    assert second == first


def test_evidence_pages_must_match_tex():
    from .page_layout import prepare_layout

    wrong = evidence()
    wrong["pages"].reverse()
    with pytest.raises(ValueError, match="page"):
        prepare_layout(SOURCE, wrong)


def test_optimizer_enforces_layout_with_existing_cached_tex(tmp_path):
    source = tmp_path / "input.tex"
    source.write_text(SOURCE)
    ev = tmp_path / "evidence.json"
    ev.write_text(json.dumps(evidence()))
    report = tmp_path / "report.json"
    result = cli.main(["optimise", str(source), "-o", str(tmp_path / "out.tex"),
        "--no-llm", "--page-layout-evidence", str(ev), "--report", str(report)])
    assert result == 0
    details = json.loads(report.read_text())
    assert details["compile_check"]["passes"] == 2
    assert details["layout_check"]["actual_pages"] == 2
    assert details["layout_check"]["ok"] is True
    assert details["tables"] == 0


def test_optimizer_keeps_pagination_findings_advisory(tmp_path):
    source = tmp_path / "input.tex"
    source.write_text(SOURCE.replace("First page body.", "Before\\newpage\nAfter"))
    ev = tmp_path / "evidence.json"
    ev.write_text(json.dumps(evidence()))
    report = tmp_path / "report.json"
    result = cli.main(["optimise", str(source), "-o", str(tmp_path / "out.tex"),
        "--no-llm", "--page-layout-evidence", str(ev), "--report", str(report)])
    assert result == 0
    details = json.loads(report.read_text())
    assert details["layout_check"]["ok"] is False
    assert details["layout_check"]["actual_pages"] == 3
    assert details["layout_check"]["attempts"] == 1


def test_pipeline_accepts_compile_success_without_strict_layout_validation(tmp_path):
    from .stages import StageCommand, _validate

    tex = tmp_path / "output.tex"
    tex.write_text("% LEXOID_PAGE_COMPLETED: 1/1\n")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"compile_check": {"ok": True, "passes": 2}}))
    stage = StageCommand("optimise", [], (), (tex, report), "test")
    _validate(stage, 1)
