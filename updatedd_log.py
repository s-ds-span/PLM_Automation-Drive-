#!/usr/bin/env python3
"""
updatedd_log.py — SPAN Drive SBX hex capture / decode tool
═══════════════════════════════════════════════════════════
Lucid's ``updatedd_log.py`` frames Modbus RTU (fixed 7/8 bytes).
Drive uses SBX with **variable PayloadLen (N)**:

  [ 0x60 | Ver(1) | RcvrID(2) | SndrID(2) | HdrCRC(2)
        | TxnID(1) | MsgTypeID(2) | PayloadLen(1)
        | Payload(N) | PayloadCRC(2) | 0x62 ]

Usage
-----
  # Decode a pasted / file hex dump (continuous stream OK)
  python3 updatedd_log.py decode sample.hex
  python3 updatedd_log.py decode -          # stdin
  python3 updatedd_log.py decode sample.hex -o decoded.txt
  python3 updatedd_log.py decode sample.hex --json

  # One-liner summary lines
  python3 updatedd_log.py decode sample.hex --summary

MsgTypeID names follow the Sbx decode sheet (Drive status, ACK 0xF000, …).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from repo root without install
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from drive_automation.sbx_decode import (  # noqa: E402
    decode_hex_text,
    format_frame_block,
)


def cmd_decode(args: argparse.Namespace) -> int:
    if args.input == "-":
        text = sys.stdin.read()
    else:
        path = Path(args.input)
        if not path.exists():
            print(f"Error: file not found: {path}", file=sys.stderr)
            return 1
        text = path.read_text(encoding="utf-8")

    frames = decode_hex_text(text)
    if not frames:
        print("No SBX frames found (need 0x60 … PayloadLen … 0x62).", file=sys.stderr)
        return 2

    if args.json:
        body = "\n".join(json.dumps(f.to_dict()) for f in frames) + "\n"
    elif args.summary:
        body = "\n".join(f.summary_line() for f in frames) + "\n"
        body += f"# {len(frames)} frame(s)\n"
    else:
        blocks = [format_frame_block(f, i + 1) for i, f in enumerate(frames)]
        blocks.append(f"=== Summary: {len(frames)} frame(s) ===")
        body = "\n\n".join(blocks) + "\n"

    if args.output:
        Path(args.output).write_text(body, encoding="utf-8")
        print(f"Wrote {len(frames)} frame(s) -> {args.output}")
    else:
        sys.stdout.write(body)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture/decode SPAN Drive SBX bus traffic (variable payload length).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dec = sub.add_parser(
        "decode",
        help="Decode hex dump file or stdin into human-readable SBX frames",
    )
    dec.add_argument(
        "input",
        help="Hex dump path, or '-' for stdin",
    )
    dec.add_argument("-o", "--output", help="Output file (default stdout)")
    dec.add_argument("--json", action="store_true", help="One JSON object per frame")
    dec.add_argument(
        "--summary",
        action="store_true",
        help="One-line summaries only",
    )
    dec.set_defaults(func=cmd_decode)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
