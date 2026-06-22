from __future__ import annotations

import argparse
import json
import os
import sys

from backend.app.services.attachment_parser import parse_image_text_in_process_with_metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated OCR worker for attachment image parsing.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--suffix", default=".png")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    os.environ["ATTACHMENT_OCR_IN_WORKER"] = "1"
    try:
        with open(args.input, "rb") as handle:
            content = handle.read()
        text, metadata = parse_image_text_in_process_with_metadata(content, args.suffix)
        write_output(args.output, {"ok": True, "text": text, "metadata": metadata})
        return 0
    except Exception as exc:
        write_output(args.output, {"ok": False, "error": str(exc)})
        return 1


def write_output(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
