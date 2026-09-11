"""Inject the macros the optimiser relies on, idempotently and without touching
anything the converter already put in the preamble."""

from __future__ import annotations

import re

from .tex_tables import mask_comments

MARK_BEGIN = "% >>> lexoid-texopt (auto-generated, safe to regenerate) >>>"
MARK_END = "% <<< lexoid-texopt <<<"

# NOTE on \SA:
#   \leavevmode\hbox{}  ->  zero width/height/depth, no glue, no mode change.
#   * In l/c/r columns we are already in restricted horizontal mode -> \leavevmode is
#     a no-op.
#   * In p/m/b columns \leavevmode starts the paragraph exactly as the first character
#     would have; \parindent is 0pt there because of \@parboxrestore.
#   The empty \hbox is what forces SyncTeX to open a box record on THIS source line.
BLOCK = r"""
\RequirePackage{amssymb}
%% --- sync anchor: zero-size box that gives SyncTeX a per-cell record -------------
\providecommand{\SA}{\leavevmode\hbox{}\relax}

%% --- handwritten field: renders #2 unchanged, registers #1 ----------------------
\makeatletter
\newif\ifhwf@dest   \hwf@desttrue      % set \hwf@destfalse to disable PDF anchors
\newwrite\hwf@out
\immediate\openout\hwf@out=\jobname.hwf\relax
\providecommand{\hwfield}[2]{%
  \SA%
  \begingroup
    \write\hwf@out{#1\string\t\noexpand\thepage}%  page = OUTPUT pdf page, at shipout
  \endgroup
  \ifhwf@dest\@ifundefined{hypertarget}{}{\hypertarget{fld:#1}{}}\fi
  #2%
}
%% --- editable checkbox ------------------------------------------------------------
%% Current Lexoid writes \fieldvalue{\checkboxfield{state}}. Historical optimized
%% sources used \checkboxfield{ID}{state}{label}; retain both forms during migration.
\def\cb@checked{checked}
\def\cb@unclear{unclear}
\newcommand{\cb@draw}[1]{%
  \begingroup
    \setlength{\fboxsep}{0.15ex}%
    \def\cb@state{#1}%
    \ifx\cb@state\cb@checked
      \fbox{\rule{0pt}{1.25ex}\makebox[1.25ex][c]{\scriptsize\ensuremath{\checkmark}}}%
    \else\ifx\cb@state\cb@unclear
      \fbox{\rule{0pt}{1.25ex}\makebox[1.25ex][c]{\scriptsize\sffamily ?}}%
    \else
      \fbox{\rule{0pt}{1.25ex}\makebox[1.25ex][c]{}}%
    \fi\fi
  \endgroup}
\providecommand{\checkboxfield}[1]{}%
\renewcommand{\checkboxfield}[1]{%
  \@ifnextchar\bgroup{\cb@legacy{#1}}{\cb@draw{#1}}}
\newcommand{\cb@legacy}[3]{\hwfield{#1}{\cb@draw{#2}\,#3}}
\AtEndDocument{\immediate\closeout\hwf@out}
\makeatother
"""


def inject(tex: str) -> str:
    """Insert (or refresh) the macro block just before \\begin{document}."""
    support = ""
    if re.search(r"\\sout\b", mask_comments(tex)):
        support = ("\\makeatletter\n"
                   "\\@ifundefined{sout}{\\RequirePackage[normalem]{ulem}}{}\n"
                   "\\makeatother\n")
    block = f"{MARK_BEGIN}\n{support}{BLOCK.strip()}\n{MARK_END}\n"

    if MARK_BEGIN in tex and MARK_END in tex:
        return re.sub(
            re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END) + r"\n?",
            lambda _: block, tex, count=1, flags=re.S)

    m = re.search(r"^\s*\\begin\{document\}", tex, re.M)
    if m:
        return tex[:m.start()] + block + tex[m.start():]

    # Fragment without a preamble (e.g. \input-ed page file): prepend.
    return block + tex
