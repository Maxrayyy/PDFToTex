"""Measure actual XeTeX boxes: checkbox states must share the text math axis."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from lexoid.core.prompt_templates import LATEX_CHECKBOX_FIELD_COMMAND
from texopt.optimization.preamble import inject


@unittest.skipUnless(shutil.which('xelatex'), 'XeLaTeX is required')
class CheckboxBaselineTests(unittest.TestCase):
    def test_states_share_size_and_axis_at_every_text_size(self):
        probe = r'''
\newsavebox{\Checked}\newsavebox{\Empty}\newsavebox{\Unclear}
\newcommand{\AssertNear}[2]{%
  \dimen0=\dimexpr#1-#2\relax
  \ifdim\dimen0<0pt \dimen0=-\dimen0\fi
  \ifdim\dimen0>.02pt \errmessage{Checkbox alignment mismatch: \the\dimen0}\fi}
\newcommand{\Probe}{%
  \sbox{\Checked}{\checkboxfield{checked}}%
  \sbox{\Empty}{\checkboxfield{unchecked}}%
  \sbox{\Unclear}{\checkboxfield{unclear}}%
  \AssertNear{\ht\Checked}{\ht\Empty}%
  \AssertNear{\dp\Checked}{\dp\Empty}%
  \AssertNear{\ht\Unclear}{\ht\Empty}%
  \AssertNear{\dp\Unclear}{\dp\Empty}%
  \AssertNear{\wd\Checked}{\wd\Empty}%
  \AssertNear{\wd\Unclear}{\wd\Empty}%
  \setbox0=\hbox{$x$}% initialise text-size math fonts
  \AssertNear{.5\ht\Checked-.5\dp\Checked}{\fontdimen22\textfont2}%
  培养基名称：\checkboxfield{checked}TSA\quad\checkboxfield{unchecked}其它\par}
\begin{document}
\tiny\Probe\scriptsize\Probe\footnotesize\Probe\small\Probe
\normalsize\Probe\large\Probe\Large\Probe
\begin{tabular}{p{5cm}l}\small\Probe & \checkboxfield{checked} TSA\end{tabular}
\end{document}
'''
        for mode in ['recognition', 'optimized', 'refreshed']:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                source = (r'\documentclass[UTF8,10pt,fontset=fandol]{ctexart}'+'\n'
                          +r'\usepackage{amssymb,etoolbox}'+'\n'
                          + LATEX_CHECKBOX_FIELD_COMMAND+'\n'+probe)
                if mode != 'recognition':
                    source = inject(source)
                if mode == 'refreshed':
                    source = inject(source)
                path = Path(directory)/'probe.tex'
                path.write_text(source)
                result = subprocess.run(['xelatex','-no-pdf','-interaction=nonstopmode',
                                         '-halt-on-error',str(path)],cwd=directory,
                                        capture_output=True,text=True,timeout=60)
                self.assertEqual(result.returncode,0,result.stdout[-2500:]+result.stderr)
                self.assertNotIn('Missing character',result.stdout)


if __name__ == '__main__':
    unittest.main()
