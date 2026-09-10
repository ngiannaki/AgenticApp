from pathlib import Path


def parse_txt(txt_path: str | Path) -> dict:
    """Read a plain-text file (.txt, .md, .log, or anything else UTF-8-ish).

    Encoding errors are replaced rather than raised — a tool meant to hand
    an LLM readable text shouldn't fail outright over a handful of stray
    bytes in an otherwise-text file.
    """

    txt_path = Path(txt_path)

    if not txt_path.exists():
        raise FileNotFoundError(f"Text file not found: {txt_path}")

    text = txt_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    return {
        "filename": txt_path.name,
        "line_count": len(lines),
        "char_count": len(text),
        "full_text": text,
    }
