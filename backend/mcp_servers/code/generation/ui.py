"""generate_ui: turns model-authored HTML/CSS/JS into a stored artifact the
frontend renders inside a sandboxed iframe.

Unlike generate_file, the input here isn't validated structured data — it's
free-form markup the model wrote itself, so there is no "correct" shape to
check beyond size limits. Nothing is executed on this server; the HTML is
saved as-is (wrapped in a minimal document skeleton if it isn't one already)
and only ever runs client-side, inside the sandboxed iframe the frontend
mounts it in — see ChatWindow.tsx's UiArtifactBlock. That sandbox (no
allow-same-origin) is the actual security boundary; this module's job is
just to size-cap the input and hand back a stable URL.
"""

from __future__ import annotations

import re
from typing import Any

from backend.mcp_servers.code.artifacts import inline_artifact
from backend.mcp_servers.code.errors import ValidationError

_FILENAME = "index.html"
_CONTENT_TYPE = "text/html"
_HAS_DOCUMENT_SHAPE = re.compile(r"<\s*(!doctype|html)\b", re.IGNORECASE)


def generate_ui(html: str, title: str = "Generated UI", *, settings: Any) -> dict[str, Any]:
    if not isinstance(html, str) or not html.strip():
        raise ValidationError("html must be a non-empty string.")
    if len(html) > settings.max_ui_html_length:
        raise ValidationError(
            f"html is {len(html)} characters, exceeding the {settings.max_ui_html_length}-character limit."
        )

    document = _ensure_document(html, title if isinstance(title, str) and title.strip() else "Generated UI")
    _check_inline_scripts(document)
    data = document.encode("utf-8")

    if len(data) > settings.max_artifact_bytes:
        raise ValidationError(f"Generated UI is {len(data)} bytes, exceeding the {settings.max_artifact_bytes}-byte limit.")

    # Returned inline; the API stores it and swaps in a URL (see artifacts.py).
    return {"success": True, **inline_artifact(data, _FILENAME, _CONTENT_TYPE)}


_INLINE_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_PAIRS = {")": "(", "]": "[", "}": "{"}
# A "/" after one of these starts a regex literal rather than a division.
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")


def _check_inline_scripts(document: str) -> None:
    """Reject pages whose inline <script> has unbalanced brackets.

    A single stray ")" makes the browser drop the whole script, so the page
    renders its static shell with every chart/KPI blank and nothing in the
    chat says why. Raising here puts the exact line in the tool result so the
    model can fix it and call generate_ui again. Deliberately narrow — only
    bracket mismatches, never style — so a valid page is not rejected.
    """
    for match in _INLINE_SCRIPT.finditer(document):
        attrs, code = match.group(1).lower(), match.group(2)
        if "src=" in attrs or ("type=" in attrs and "javascript" not in attrs and "module" not in attrs):
            continue
        problem = _bracket_problem(code)
        if problem:
            raise ValidationError(
                f"The page's <script> has a JavaScript syntax error and would not run: {problem} "
                "Fix it and call generate_ui again."
            )


def _bracket_problem(code: str) -> str | None:
    """Return a description of the first bracket mismatch in `code`, or None."""
    stack: list[tuple[str, int]] = []  # (opener, line); a "`" entry is a template literal
    line, i, n = 1, 0, len(code)
    prev = ""  # last significant (non-space) character outside strings/comments
    while i < n:
        c = code[i]
        in_template = bool(stack) and stack[-1][0] == "`"
        if c == "\n":
            line += 1
        if in_template:
            if c == "\\":
                i += 1
            elif c == "`":
                stack.pop()
                prev = "`"
            elif c == "$" and code.startswith("${", i):
                stack.append(("${", line))
                i += 1
            i += 1
            continue
        if c in " \t\r\n":
            i += 1
            continue
        if code.startswith("//", i):
            i = code.find("\n", i)
            i = n if i < 0 else i
            continue
        if code.startswith("/*", i):
            end = code.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += code.count("\n", i, end)
            i = end
            continue
        if c in "'\"":
            j = i + 1
            while j < n and code[j] != c and code[j] != "\n":
                j += 2 if code[j] == "\\" else 1
            i, prev = j + 1, c
            continue
        if c == "`":
            stack.append(("`", line))
        elif c == "/" and (not prev or prev in _REGEX_PREV):
            j, in_class = i + 1, False
            while j < n and code[j] != "\n" and (in_class or code[j] != "/"):
                if code[j] == "\\":
                    j += 1
                elif code[j] == "[":
                    in_class = True
                elif code[j] == "]":
                    in_class = False
                j += 1
            i, prev = j + 1, "/"
            continue
        elif c in "([{":
            stack.append((c, line))
        elif c in ")]}":
            if c == "}" and stack and stack[-1][0] == "${":
                stack.pop()  # back into the enclosing template literal
            elif not stack or stack[-1][0] != _PAIRS[c]:
                found = f"'{stack[-1][0]}' opened on script line {stack[-1][1]}" if stack else "nothing open"
                return f"unexpected '{c}' on script line {line} ({found}) — check for an extra or missing bracket."
            else:
                stack.pop()
        prev = c
        i += 1
    if stack:
        opener, opened = stack[-1]
        return f"'{opener}' opened on script line {opened} is never closed."
    return None


def _ensure_document(html: str, title: str) -> str:
    """Wrap a bare fragment in a minimal HTML document; pass a full document through untouched."""
    if _HAS_DOCUMENT_SHAPE.search(html):
        return html
    escaped_title = title.replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escaped_title}</title>\n</head>\n<body>\n{html}\n</body>\n</html>\n"
    )
