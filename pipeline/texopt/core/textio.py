"""Encoding-safe LaTeX I/O with UTF-8 normalization and atomic writes."""

from __future__ import annotations

import codecs
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    had_bom: bool = False


def read_text_auto(path: str | Path) -> DecodedText:
    raw = Path(path).read_bytes()
    bom_candidates = (
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    )
    for bom, encoding in bom_candidates:
        if raw.startswith(bom):
            text = raw.decode(encoding, errors="strict")
            _reject_nul(text, path)
            return DecodedText(text, encoding, True)

    failures = []
    for encoding in ("utf-8", "gb18030", "big5"):
        try:
            text = raw.decode(encoding, errors="strict")
            _reject_nul(text, path)
            return DecodedText(text, encoding, False)
        except (UnicodeDecodeError, ValueError) as exc:
            failures.append(f"{encoding}: {exc}")
    raise UnicodeError(
        f"cannot decode {path}; tried UTF-8, GB18030 and Big5: " + "; ".join(failures)
    )


def write_utf8_atomic(path: str | Path, text: str) -> Path:
    """Write UTF-8 without BOM, replacing the destination only after a full write."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(text.encode("utf-8", errors="strict"))
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return target


def _reject_nul(text: str, path: str | Path) -> None:
    if "\x00" in text:
        raise ValueError(f"decoded text contains NUL bytes: {path}")
