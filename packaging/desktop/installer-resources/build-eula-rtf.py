#!/usr/bin/env python3
"""Generate EULA.rtf for the macOS .pkg installer.

Usage:
    python3 build-eula-rtf.py <LICENSE_path> <EULA.rtf_path>

Pipeline:
  1. Read LICENSE, split on blank lines into paragraphs, join each
     paragraph's continuation lines into one long line so the installer
     view renders reflowed text instead of hard-wrapped 72-char lines.
  2. Append ACKNOWLEDGMENT OF RISK block if not already in LICENSE.
  3. Emit RTF with the ACKNOWLEDGMENT header bold at 16pt and the body
     bold at 14pt; everything else is 12pt Helvetica.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ACK_HDR = "ACKNOWLEDGMENT OF RISK"
_ACK_BODY = (
    "I FULLY UNDERSTAND THAT AI AGENTS CAN DAMAGE CODE AND SYSTEMS. "
    "I UNDERSTAND THAT THESE ARE EXPERIMENTAL, PROBABILISTIC SYSTEMS "
    "THAT CAN MISINTERPRET INSTRUCTIONS AND MAKE MISTAKES."
)

# RTF preamble matching textutil's cocoartf output format.
_PREAMBLE = (
    "{\\rtf1\\ansi\\ansicpg1252\\cocoartf2822\n"
    "\\cocoatextscaling0\\cocoaplatform0"
    "{\\fonttbl\\f0\\fswiss\\fcharset0 Helvetica;}\n"
    "{\\colortbl;\\red255\\green255\\blue255;}\n"
    "{\\*\\expandedcolortbl;;}\n"
    "\\pard\\tx560\\tx1120\\tx1680\\tx2240\\tx2800\\tx3360\\tx3920"
    "\\tx4480\\tx5040\\tx5600\\tx6160\\tx6720"
    "\\pardirnatural\\partightenfactor0\n"
    "\\f0\\fs24 \\cf0 "
)


def _rtf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _rtf_line(text: str, bold: bool = False, fs: int = 24) -> str:
    escaped = _rtf_escape(text)
    if bold:
        return "{\\b\\fs%d %s}\\\n" % (fs, escaped)
    return "%s\\\n" % escaped


def _unwrap(text: str) -> list[str]:
    blocks = re.split(r"\n{2,}", text.strip())
    return [" ".join(b.split("\n")) for b in blocks if b.strip()]


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: build-eula-rtf.py <LICENSE> <EULA.rtf>", file=sys.stderr)
        sys.exit(1)

    license_path, out_path = Path(sys.argv[1]), Path(sys.argv[2])
    if not license_path.is_file():
        print(f"LICENSE missing: {license_path}", file=sys.stderr)
        sys.exit(1)

    paras = _unwrap(license_path.read_text())

    if not any(_ACK_HDR in p for p in paras):
        paras += [_ACK_HDR, _ACK_BODY]

    lines: list[str] = [_PREAMBLE]
    for i, para in enumerate(paras):
        if i > 0:
            lines.append("\\\n")  # blank separator between paragraphs
        if para == _ACK_HDR:
            lines.append(_rtf_line(para, bold=True, fs=32))
        elif para.startswith("I FULLY UNDERSTAND THAT AI AGENTS"):
            lines.append(_rtf_line(para, bold=True, fs=28))
        else:
            lines.append(_rtf_line(para))

    lines.append("}")

    out_path.write_text("".join(lines), encoding="utf-8")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
