import json
import subprocess
import xml.etree.ElementTree as ET

import pytest

from .page_fallback import compile_page, upgrade_pages
from .test_page_fallback import sample


# LaTeX 2026/06/01 text superscript path from latex2e/base/ltfloat.dtx.
# Exercise its text-mode hskip inside ulem even on older container formats.
TEXT_SCRIPTS_2026 = r"""
\makeatletter
\protected\def\@textsuperscript#1{%
  \check@mathfonts\leavevmode\begingroup
  \sbox\z@{\fontsize\sf@size\sf@size#1}%
  \raise\dimexpr\textsuperscript@offset\relax\box\z@
  \textsuperscript@space\endgroup}
\def\textsuperscript@offset{%
  \ifdim\fontdimen14\textfont\tw@<\dimexpr\dp\z@+0.25\fontdimen5\textfont\tw@\relax
    \dp\z@+0.25\fontdimen5\textfont\tw@
  \else\fontdimen14\textfont\tw@\fi}
\def\textsuperscript@space{\nobreak\hskip\scriptspace\kern\z@}
\makeatother
"""


def test_ulem_superscript_compiles_with_2026_text_implementation(tmp_path):
    from .local_tex import normalize_tex

    source = (r"\documentclass{article}\usepackage[normalem]{ulem}"
        + TEXT_SCRIPTS_2026 + r"\begin{document}\begin{tabular}{p{4cm}l}"
        + r"\sout{Pierce\textsuperscript{TM} Rapid Gold BCA Protein Assay Kit}"
        + r" & Next\\\end{tabular}\end{document}")
    assert compile_page(source, tmp_path / "before")
    assert "Extra }, or forgotten" in (tmp_path / "before/compile.log").read_text()
    fixed, changes = normalize_tex(source)
    assert changes.get("ulem_text_scripts") == 1
    assert not compile_page(fixed, tmp_path / "after")
    assert r"\sout{Pierce\mbox{\textsuperscript{TM}} Rapid Gold" in fixed
    assert normalize_tex(fixed)[0] == fixed


def test_ulem_script_repair_preserves_fields_and_ignores_literals_and_boxes():
    from .local_tex import normalize_tex

    protected = (r"Outside \textsuperscript{TM} \verb|\sout{\textsuperscript{TM}}|"
        + "\n% \\sout{\\textsuperscript{TM}}\n"
        + r"\newcommand{\custom}[1]{\sout{\textsuperscript{#1}}}"
        + r"\sout{\mbox{\textsuperscript{TM}}}")
    field = "% #VALUE_ID: LEX-P0001-V0001\n% #FIELD_VALUE: Label\n"
    source = protected + r"\uline{" + field + r"\fieldvalue{X}\textsubscript{2}}"
    fixed, changes = normalize_tex(source)
    assert protected in fixed and field in fixed
    assert r"\fieldvalue{X}\mbox{\textsubscript{2}}" in fixed
    assert changes.get("ulem_text_scripts") == 1


@pytest.mark.parametrize("space", [r"\vspace{3cm}", r"\vspace*{3cm}",
    r"\rule[-\dimexpr3cm-\ht\strutbox\relax]{0pt}{3cm}"])
def test_marked_experimental_spaces_become_frames(space):
    from .local_tex import normalize_tex

    source = "% LEXOID_OMITTED_EXPERIMENTAL_FIGURE\n" + space + r" & Signature\\"
    fixed, changes = normalize_tex(source)
    assert changes.get("experimental_figure_frames") == 1
    assert r"\LexoidExperimentalFigure{\linewidth}{3cm}" in fixed
    assert fixed.endswith(r" & Signature\\")
    assert normalize_tex(fixed)[0] == fixed
    assert normalize_tex(space)[0] == space


def test_experimental_frame_handles_trailing_marker_and_preserves_metadata(tmp_path):
    from .local_tex import normalize_tex

    cell = (r"\vspace{3cm}% LEXOID_OMITTED_EXPERIMENTAL_FIGURE" + "\n"
        + r"\par Signature \fieldvalue{Alice}")
    source = (r"\documentclass{article}\begin{document}"
        + r"\begin{tabular}{|p{4cm}|p{4cm}|}\hline " + cell + " & " + cell
        + r"\\\hline\end{tabular}\end{document}")
    fixed, changes = normalize_tex(source)
    assert changes.get("experimental_figure_frames") == 2
    assert fixed.count(r"Signature \fieldvalue{Alice}") == 2
    assert not compile_page(fixed, tmp_path / "frames")
    assert normalize_tex(fixed)[0] == fixed


SPLIT_FORM_ROW = (
    r"\begin{tabular}{|p{1cm}|p{2cm}|p{3cm}|p{3cm}|}\hline" + "\n"
    r"\multicolumn{2}{|p{3cm}|}{Instructions} & Room \fieldvalue{R101}\\" + "\n"
    "% #VALUE_ID: LEX-P0001-V0002\n% #FIELD_VALUE: Cabinet\n"
    r"Cabinet \fieldvalue{1435001}\\" + "\n"
    r"Volume \fieldvalue{20} & Operator \fieldvalue{Alice}\\" + "\n"
    r"\fieldvalue{2026.03.23}\\\hline\end{tabular}"
)


def test_split_form_row_restores_cells_without_changing_field_metadata():
    from .local_tex import normalize_tex

    fixed, changes = normalize_tex(SPLIT_FORM_ROW)
    assert changes.get("split_paragraph_rows") == 3
    assert r"R101}\newline{}" in fixed
    assert r"1435001}\newline{}" in fixed
    assert r"Alice}\newline{}" in fixed
    assert "% #VALUE_ID: LEX-P0001-V0002\n% #FIELD_VALUE: Cabinet\n" in fixed
    assert r"2026.03.23}\\\hline" in fixed
    assert normalize_tex(fixed)[0] == fixed


@pytest.mark.parametrize("body", [
    r"\hline A & B\\ C & D\\\hline",
    r"\hline A & B\\ \fieldvalue{C}\\\hline",
    r"\hline A & \multirow{2}{*}{B}\\ \fieldvalue{C}\\\hline",
    r"\hline A & B\\[2pt] \fieldvalue{C}\\\hline",
    r"\hline A & B\tabularnewline \fieldvalue{C}\\\hline",
])
def test_complete_sparse_and_explicit_rows_are_not_merged(body):
    from .local_tex import normalize_tex

    source = r"\begin{tabular}{p{2cm}p{3cm}}" + body + r"\end{tabular}"
    assert normalize_tex(source)[0] == source


NESTED_FORM_ROW = (
    r"\begin{tabular}{|p{1cm}|p{5cm}|p{4cm}|}\hline "
    r"\multirow{1}{=}{Review} & \rule{0pt}{4.5cm}Occurred?\\[4pt] "
    r"Closed?\\[4pt] Impact? & "
    r"\begin{tabular}{@{}p{1.8cm}|p{1.8cm}@{}}"
    r"\fieldvalue{YES} & \fieldvalue{CLOSED}\\\hline "
    r"\fieldvalue{RECORD} & \fieldvalue{SAFE}"
    r"\end{tabular}\\\hline\end{tabular}"
)


def test_nested_form_instructions_are_rejoined_without_changing_result_rows():
    from .local_tex import normalize_tex
    from .syntax_check import validate_latex
    from .syntax_repair import repair_invariant_violations

    fixed, changes = normalize_tex(NESTED_FORM_ROW)
    assert changes.get("split_paragraph_rows") == 2
    assert not [i for i in validate_latex(fixed) if i.code.startswith("TABLE_ALIGNMENT_")]
    assert repair_invariant_violations(NESTED_FORM_ROW, fixed) == []
    assert r"\fieldvalue{CLOSED}\\\hline" in fixed
    assert "[4pt]" not in fixed
    assert normalize_tex(fixed)[0] == fixed


@pytest.mark.parametrize("old,new", [
    (r"\multirow{1}", r"\multirow{2}"),
    (r"\multirow{1}{=}{Review}", "Review"),
    (r"Closed?\\[4pt]", r"Closed?\tabularnewline"),
    (r"Closed?\\[4pt]", r"Closed?\\[4pt] &"),
    (r"p{5cm}", "l"),
])
def test_ambiguous_nested_form_rows_are_unchanged(old, new):
    from .local_tex import normalize_split_paragraph_rows

    source = NESTED_FORM_ROW.replace(old, new)
    assert normalize_split_paragraph_rows(source) == (source, 0)


def test_nested_form_results_render_in_result_column(tmp_path):
    from .local_tex import normalize_tex

    source = (r"\documentclass{article}\usepackage{array,multirow}"
              r"\newcommand{\fieldvalue}[1]{#1}\begin{document}" + NESTED_FORM_ROW
              + r"\end{document}")
    fixed, _ = normalize_tex(source)
    path = tmp_path / "nested-form.tex"
    path.write_text(fixed)
    compiled = subprocess.run(["xelatex", "-interaction=nonstopmode", "-halt-on-error", path.name],
                              cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stdout[-2000:]
    bbox = subprocess.run(["pdftotext", "-bbox", str(path.with_suffix(".pdf")), "-"],
                          check=True, capture_output=True, text=True, timeout=10)
    words = {node.text: node.attrib for node in ET.fromstring(bbox.stdout).iter()
             if node.tag.endswith("}word")}
    instructions = [float(words[word]["xMin"]) for word in ("Occurred?", "Closed?", "Impact?")]
    assert max(instructions) - min(instructions) < 1
    assert float(words["YES"]["xMin"]) > instructions[0] + 100
    assert float(words["RECORD"]["xMin"]) >= float(words["YES"]["xMin"]) - 1


def test_heading_ends_before_block_table_but_inline_and_nested_tables_remain():
    from .local_tex import normalize_tex

    block = "\\par\\noindent 1.1. Equipment:\n\\noindent\\begin{tabular}{ll}A & B\\\\\\end{tabular}"
    fixed, changes = normalize_tex(block)
    assert changes.get("table_heading_breaks") == 1
    assert "Equipment:\n\\par\\noindent\\begin{tabular}" in fixed
    assert normalize_tex(fixed)[0] == fixed
    for source in (
        r"Inline: \begin{tabular}{ll}A & B\\\end{tabular}",
        "\\par\\noindent Equipment:\n\n\\noindent\\begin{tabular}{ll}A & B\\\\\\end{tabular}",
        r"\begin{tabular}{ll}Label & \begin{tabular}{l}A\\B\end{tabular}\\\end{tabular}",
    ):
        assert normalize_tex(source)[0] == source


def test_rendered_fields_stay_in_their_column_and_heading_is_above_table(tmp_path):
    from .local_tex import normalize_tex

    source = (r"\documentclass{article}\begin{document}" + "\n"
              + SPLIT_FORM_ROW + "\n\\par\\noindent 1.1. Equipment:\n"
              + r"\noindent\begin{tabular}{|p{4cm}|}\hline Instrument\\\hline\end{tabular}"
              + "\n\\end{document}")
    fixed, _ = normalize_tex(source)
    path = tmp_path / "layout.tex"
    path.write_text(fixed)
    compiled = subprocess.run(["xelatex", "-interaction=nonstopmode", "-halt-on-error", path.name],
                              cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stdout[-2000:]
    bbox = subprocess.run(["pdftotext", "-bbox", str(path.with_suffix(".pdf")), "-"],
                          check=True, capture_output=True, text=True, timeout=10)
    words = {node.text: node.attrib for node in ET.fromstring(bbox.stdout).iter()
             if node.tag.endswith("}word")}
    assert abs(float(words["Room"]["xMin"]) - float(words["Cabinet"]["xMin"])) < 1
    assert float(words["Cabinet"]["xMin"]) > float(words["Instructions"]["xMin"]) + 60
    assert float(words["2026.03.23"]["xMin"]) >= float(words["Operator"]["xMin"])
    assert float(words["Instrument"]["yMin"]) > float(words["Equipment:"]["yMax"])


@pytest.mark.parametrize("body", [
    r"\begin{tabular}{p{4cm}}control\newlineIL-6-0002\end{tabular}",
    r"\fbox{\begin{minipage}{3cm}\centering Original\\\hline Copy\end{minipage}}",
    r"\begin{tikzpicture}\draw (0,0) rectangle (1,1);\end{tikzpicture}",
    r"\begin{picture}(145,145)\put(36,49){\line(23,-30){23}}\end{picture}",
    r"\begin{picture}(40,40)\put(0,0){\vector(23,30){23}}\end{picture}",
    r"\begin{tabular}{p{2cm}p{2cm}}a & \raggedleft b\\\hline c & d\\\end{tabular}",
])
def test_known_local_errors_are_repaired_before_any_paid_upgrade(tmp_path, body):
    source, raw, evidence = sample(tmp_path)
    original = (r"\documentclass{article}\begin{document}" + "\n" + body +
                "\n% LEXOID_PAGE_COMPLETED: 1/3\n"
                "healthy\n% LEXOID_PAGE_COMPLETED: 2/3\n"
                "healthy\n% LEXOID_PAGE_COMPLETED: 3/3\n\\end{document}\n")
    assert any(i.severity == "error" for i in compile_page(original, tmp_path / "before"))
    raw.write_text(original)
    calls = []

    def recognize(*args):
        calls.append(1)
        raise AssertionError("Local syntax must not trigger model recognition")

    report = upgrade_pages(source, raw, evidence, tmp_path / "cache", "gpt-5.6-sol",
                           "gpt-6-astra", recognize=recognize, renderer=lambda *args: None)
    assert calls == []
    assert report["upgraded"] == []
    assert report["local_repaired"] == [1]
    assert not compile_page(raw.read_text(), tmp_path / "after")
    checkpoints = list((tmp_path / "cache").rglob("local-repair.json"))
    assert checkpoints
    audit = json.loads(checkpoints[0].read_text())
    assert audit["rules"] and audit["original_sha256"] != audit["normalized_sha256"]


def test_shared_normalization_is_idempotent_and_keeps_fields_and_nested_tables():
    from .local_tex import normalize_tex

    fields = "% #VALUE_ID: LEX-P0001-V0001\n% #FIELD_VALUE: Original label\n\\fieldvalue{23.5}"
    table = r"\begin{tabular}{ll}a & \begin{tabular}{l}b\\\hline c\end{tabular}\\\hline\end{tabular}"
    src = (r"\documentclass{article}\begin{document}" + "\n" + fields + table +
           "\n% LEXOID_PAGE_COMPLETED: 1/1\n\\end{document}\n")
    fixed, changes = normalize_tex(src)
    assert fields in fixed and table in fixed
    assert changes
    assert normalize_tex(fixed)[0] == fixed


def test_dependencies_discovered_on_later_page_are_added_to_final_preamble(tmp_path):
    source, raw, evidence = sample(tmp_path)
    original = (r"\documentclass{article}\begin{document}" +
        "\nfirst\n% LEXOID_PAGE_COMPLETED: 1/3\n" +
        r"\begin{tikzpicture}\draw (0,0) rectangle (1,1);\end{tikzpicture}" +
        "\n% LEXOID_PAGE_COMPLETED: 2/3\nlast\n% LEXOID_PAGE_COMPLETED: 3/3\n\\end{document}\n")
    raw.write_text(original)
    calls = []
    result = upgrade_pages(source, raw, evidence, tmp_path / "cache", "gpt-5.6-sol",
                           "gpt-6-astra", recognize=lambda *args: calls.append(1),
                           renderer=lambda *args: None)
    assert calls == [] and result["unresolved"] == []
    assert not compile_page(raw.read_text(), tmp_path / "final")
