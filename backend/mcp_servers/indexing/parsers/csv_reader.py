import csv
from pathlib import Path


def parse_csv(csv_path: str | Path) -> dict:
    """Parse a CSV file into headers + rows, and a markdown-table rendering.

    The stdlib `csv` module's dialect sniffer picks the delimiter (comma,
    semicolon, tab, ...) instead of assuming a comma, since exported CSVs
    (Excel especially) often use one of the others.
    """

    csv_path = Path(csv_path)

    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(8192)
        f.seek(0)

        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel

        reader = csv.reader(f, dialect)
        all_rows = [row for row in reader if row]

    headers = all_rows[0] if all_rows else []
    rows = all_rows[1:]

    return {
        "filename": csv_path.name,
        "row_count": len(rows),
        "column_count": len(headers),
        "headers": headers,
        "rows": rows,
        "markdown": _to_markdown_table(headers, rows),
        "text": _to_plain_text(headers, rows),
    }


def _to_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    if not headers:
        return "\n".join(" | ".join(row) for row in rows)

    lines = [
        " | ".join(headers),
        " | ".join("---" for _ in headers),
    ]
    lines.extend(" | ".join(row) for row in rows)
    return "\n".join(lines)


def _to_plain_text(headers: list[str], rows: list[list[str]]) -> str:
    lines = [", ".join(headers)] if headers else []
    lines.extend(", ".join(row) for row in rows)
    return "\n".join(lines)
