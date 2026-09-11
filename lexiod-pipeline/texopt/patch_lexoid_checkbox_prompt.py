"""Idempotently add editable-checkbox rules to Lexoid's bundled LaTeX prompt."""

from __future__ import annotations

import importlib
from pathlib import Path
import re


MARKER = "Editable checkbox annotation is MANDATORY"
PREAMBLE_MARKER = r"\newcommand{{\checkboxfield}}"
OLD_TABLE_RULE = (
    r"- Tables: prefer tabularx with X columns to fit within \textwidth; if wide, "
    r"first try \small; use \resizebox{\textwidth}{!}{...} only if essential. "
)
NEW_TABLE_RULE = (
    r"- Tables: prefer plain tabular with explicit p{<width>} columns. Choose widths "
    r"whose total, including tabcolsep and rules, fits within \textwidth. Do not use "
    r"X columns. Use tabularx only as an intermediate fallback when an exact plain-"
    r"tabular width cannot be determined; the downstream optimizer will measure and "
    r"convert it. If wide, first try \small; use "
    r"\resizebox{\textwidth}{!}{...} only if essential. "
)

CHECKBOX_RULES = r"""
- Editable checkbox annotation is MANDATORY:
  * Treat EVERY checkbox option as an editable boolean field, including both selected
    and unselected options. Never emit raw `\square`, `\Box`, `\boxtimes`, or
    `\checkmark` symbols for checkbox semantics.
  * Allocate every option its own stable `% #VALUE_ID` in the normal visual sequence,
    followed by `% #FIELD_VALUE: <option label>`.
  * Render each option exactly as
    `\checkboxfield{<VALUE_ID>}{checked|unchecked}{<visible option label>}`.
  * Use `checked` only when the source visibly contains a tick/cross/filled selection;
    otherwise use `unchecked`. Keep the label in the third argument, never in the
    boolean state. A handwritten tick changes the state to `checked`; do not encode
    the tick itself as a separate text value.
"""


def patch_source(source: str, *, path: Path | str = "prompt_templates.py") -> str:
    """Return a patched prompt module, tolerating wording changes in table rules."""
    if MARKER not in source:
        anchor = "- Handwritten-entry annotation is MANDATORY:"
        if anchor not in source:
            raise RuntimeError(f"Lexoid prompt anchor not found in {path}")
        source = source.replace(anchor, CHECKBOX_RULES + anchor, 1)
    if PREAMBLE_MARKER not in source:
        anchor = r"\newcommand{{\handwritten}}[1]{{#1}}"
        if anchor not in source:
            raise RuntimeError(f"Lexoid preamble anchor not found in {path}")
        checkbox_macro = (
            anchor + "\n" +
            r"\newcommand{{\checkboxfield}}[3]{{%" + "\n" +
            r"  \ifstrequal{{#2}}{{checked}}" +
            r"{{\fbox{{\makebox[1.1ex][c]{{\scriptsize\sffamily x}}}}}}" +
            r"{{\fbox{{\makebox[1.1ex][c]{{\strut}}}}}}\,#3%" + "\n" +
            r"}}"
        )
        source = source.replace(anchor, checkbox_macro, 1)
    if OLD_TABLE_RULE in source:
        source = source.replace(OLD_TABLE_RULE, NEW_TABLE_RULE, 1)
    elif "prefer plain tabular with explicit" not in source:
        # Lexoid occasionally changes the wording of this bullet.  Match the
        # semantic, line-level anchor instead of pinning the build to one exact
        # upstream sentence.  Requiring exactly one match keeps this fail-closed.
        table_rules = list(re.finditer(r"(?m)^(?P<indent>[ \t]*)- Tables:[^\r\n]*", source))
        if len(table_rules) != 1:
            raise RuntimeError(
                f"Expected exactly one Lexoid table-rule anchor in {path}; "
                f"found {len(table_rules)}"
            )
        match = table_rules[0]
        replacement = match.group("indent") + NEW_TABLE_RULE.rstrip()
        source = source[:match.start()] + replacement + source[match.end():]
    return source


def main() -> None:
    module = importlib.import_module("lexoid.core.prompt_templates")
    path = Path(module.__file__).resolve()
    source = patch_source(path.read_text("utf-8"), path=path)
    path.write_text(source, "utf-8")


if __name__ == "__main__":
    main()
