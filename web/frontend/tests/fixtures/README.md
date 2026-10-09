# Chinese PDF preview regression

`chinese.pdf` is a synthetic XeLaTeX fixture containing only invented test text.
It embeds Fandol Chinese fonts with CID CMaps, reproducing missing Chinese glyphs
when PDF.js is started without `cMapUrl` / `cMapPacked`. The browser regression
checks actual ink in the first (Chinese-only) line and rejects font warnings.

Regenerate from `chinese.tex` with XeLaTeX (TeX Live and Fandol fonts installed):

```sh
xelatex -no-shell-escape -interaction=batchmode -halt-on-error chinese.tex
```

Keep only the `.tex` and `.pdf` files; auxiliary compilation files are unnecessary.
