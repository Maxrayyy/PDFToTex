from texopt.optimization.syntax_check import validate_latex
from texopt.optimization.syntax_repair import normalize_math_blank_lines


def test_unclosed_math_is_rejected_at_its_source_page():
    source = (
        "\\LexoidPageStart{113}{595bp}{842bp}{0}\n"
        "\\fieldvalue{100\\%}$\n"
        "\\LexoidPageEnd{113}\n"
        "\\LexoidPageStart{114}{595bp}{842bp}{0}\n"
        "x$\n\\LexoidPageEnd{114}\n"
    )
    issues = [issue for issue in validate_latex(source) if issue.code == "UNCLOSED_MATH"]
    assert [issue.line for issue in issues] == [2, 5]
    assert all(issue.severity == "error" for issue in issues)


def test_valid_math_comments_verbatim_and_escaped_dollars_are_preserved():
    source = (
        "$4\\times10^{7}/\n2$\n"
        "$$a+b$$ \\(c+d\\) \\[e+f\\]\n"
        "\\$100 % $\\(\n"
        "\\verb|$|\n"
        "\\begin{verbatim}\n$\n\\end{verbatim}\n"
    )
    assert not [issue for issue in validate_latex(source) if issue.severity == "error"]


def test_end_of_fragment_unclosed_math_is_not_reported_as_safe():
    issues = validate_latex("value $x")
    assert any(issue.code == "UNCLOSED_MATH" and issue.line == 1 for issue in issues)


def test_recognition_page_comments_also_bound_math_state():
    source = "$x\n% LEXOID_PAGE_COMPLETED: 1/2\ny$\n% LEXOID_PAGE_COMPLETED: 2/2\n"
    issues = [issue for issue in validate_latex(source) if issue.code == "UNCLOSED_MATH"]
    assert [issue.line for issue in issues] == [1, 3]


def test_orphan_math_cannot_erase_paragraphs_across_source_pages():
    source = "$x\n\n% LEXOID_PAGE_COMPLETED: 1/2\n\ntext $y$\n"
    assert normalize_math_blank_lines(source) == (source, 0)
