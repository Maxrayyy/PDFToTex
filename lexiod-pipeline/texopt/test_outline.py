import re

from .outline import normalize_outline


def document(body):
    return "\\documentclass{article}\n\\begin{document}\n" + body + "\n\\end{document}\n"


def test_numbered_headings_have_editor_visible_levels_and_keep_numbering():
    source = document(r"""\noindent{\large\textbf{1 Labels}}
\noindent\textbf{1.1 Confirmation}
\textbf{1.1.1 Equipment}
\subsubsection*{2.2 Attachment}
\subsection*{3 Filling}
\subsubsection*{3.1 Confirmation}
\subsubsection*{3.1.1 Equipment}""")
    fixed, report = normalize_outline(source)
    assert re.findall(r"\\((?:sub)*section)\*\{([^{}]+)\}", fixed) == [
        ("section", "1 Labels"), ("subsection", "1.1 Confirmation"),
        ("subsubsection", "1.1.1 Equipment"), ("subsection", "2.2 Attachment"),
        ("section", "3 Filling"), ("subsection", "3.1 Confirmation"),
        ("subsubsection", "3.1.1 Equipment")]
    assert len(report["headings"]) == 7
    assert normalize_outline(fixed)[0] == fixed


def test_tables_fields_steps_dates_and_comments_are_not_headings():
    body = r"""% \textbf{1 Comment}
\begin{tabular}{ll}\textbf{1 Item} & Value\\\end{tabular}
\fieldvalue{\textbf{2 Value}}
\textbf{1) Step}
\textbf{2026 Report}
\textbf{Company header}
Normal text \textbf{3 inline value}
\begin{verbatim}
\textbf{4 Example}
\end{verbatim}"""
    source = document(body)
    assert normalize_outline(source)[0] == source


def test_four_levels_spacing_and_partial_bold_headings():
    source = document(r"""\noindent{\large 1 Labels}
\noindent{\large 1.1\quad Confirmation}
\noindent\textbf{3\quad Filling}
\textbf{3.4.5} Module
\textbf{4.1.1.1 Equipment}
\textbf{5.Balance}""")
    fixed, report = normalize_outline(source)
    assert [(x["number"], x["level"]) for x in report["headings"]] == [
        ("1", "section"), ("1.1", "subsection"), ("3", "section"),
        ("3.4.5", "subsubsection"), ("4.1.1.1", "paragraph"), ("5", "section")]
    assert r"\subsubsection*{3.4.5 Module}" in fixed
    assert normalize_outline(fixed)[0] == fixed


def test_plain_numbered_heading_with_same_parent_as_styled_siblings():
    source = document(r"""\textbf{4.2.1 Confirmation}
\par\noindent 4.2.2 Module
\textbf{4.2.3 Module}
\noindent 1. Signature:
\noindent 9.1 Unrelated instruction""")
    fixed, report = normalize_outline(source)
    assert r"\subsubsection*{4.2.2 Module}" in fixed
    assert [h["number"] for h in report["headings"]] == ["4.2.1", "4.2.2", "4.2.3"]
    assert normalize_outline(fixed)[0] == fixed


def test_editable_fields_inside_heading_are_not_duplicated_or_moved():
    field = "% #VALUE_ID: LEX-P0001-V0001\n\\fieldvalue{13}"
    source = document("\\noindent\\textbf{2\\quad Operation (Start time\n" + field + ")}")
    fixed, report = normalize_outline(source)
    assert fixed.count(field) == 1
    assert r"\section*{2 Operation}" in fixed
    assert len(report["headings"]) == 1
    assert normalize_outline(fixed)[0] == fixed


def test_pdf_layout_is_identical_after_outline_normalization(tmp_path):
    import subprocess
    import pypdfium2 as pdfium

    source = document(r"""\noindent{\large\textbf{1 Labels}}\par
First paragraph.
\subsubsection*{2.2 Attachment}
Second paragraph.
\subsection*{3 Filling}
Third paragraph.
\noindent\textbf{3.1 Confirmation}\\
Next line.
\par\noindent{\large 4.1\quad Confirmation}\par
\textbf{4.1.1.1 Equipment}\par
\noindent 4.1.1.2 Materials
\textbf{4.1.2} Module\par
\providecommand{\fieldvalue}[1]{#1}
\noindent\textbf{5\quad Operation (Start time \underline{\fieldvalue{13}}:\underline{39})}\par
""")
    fixed, _ = normalize_outline(source)
    rendered = []
    for name, tex in (("before", source), ("after", fixed)):
        path = tmp_path / (name + ".tex")
        path.write_text(tex)
        result = subprocess.run(["xelatex", "-interaction=nonstopmode", "-halt-on-error",
                                 path.name], cwd=tmp_path, capture_output=True)
        assert result.returncode == 0, result.stdout.decode(errors="replace")[-2000:]
        doc = pdfium.PdfDocument(str(path.with_suffix(".pdf")))
        rendered.append([page.render(scale=1).to_pil().tobytes() for page in doc])
        doc.close()
    assert rendered[0] == rendered[1]
