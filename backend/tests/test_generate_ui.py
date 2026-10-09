"""generate_ui rejects pages whose inline script has unbalanced brackets."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.mcp_servers.code.errors import ValidationError
from backend.mcp_servers.code.generation.ui import generate_ui

SETTINGS = SimpleNamespace(max_ui_html_length=100_000, max_artifact_bytes=5_000_000)


def _page(js: str) -> str:
    return f"<!doctype html><html><body><svg id='c'></svg><script>{js}</script></body></html>"


def test_extra_closing_paren_is_rejected_with_a_line_number() -> None:
    # The real failure: bars() ended with `})))}` instead of `}))}`, so no chart rendered.
    js = (
        "function bars(data,keys){\n"
        "  data.forEach((d,i)=>keys.forEach((k,j)=>{\n"
        "    console.log(d[k])\n"
        "  })))\n"
        "}\n"
    )
    with pytest.raises(ValidationError, match=r"unexpected '\)' on script line 4"):
        generate_ui(_page(js), settings=SETTINGS)


def test_unclosed_brace_is_rejected() -> None:
    with pytest.raises(ValidationError, match="never closed"):
        generate_ui(_page("function f(){ if (1) { return 1; }"), settings=SETTINGS)


@pytest.mark.parametrize("js", [
    "const a = [1,2,3].map(x => ({v: x*2}));",
    "const s = ')))' + \"}}}\" + 'it\\'s (';",
    "const t = `a ${ fn({x: `inner ${y}`}) } b )`;",
    "const r = /[)}]+\\//g.test(s); const q = a / b / c;",
    "// stray ) in a comment\n/* and } here */ let z = 1;",
    "svg.innerHTML += `<text x='${100+i*220}'>${x}</text>`;",
])
def test_valid_javascript_passes(js: str) -> None:
    assert generate_ui(_page(js), settings=SETTINGS)["success"] is True


def test_external_and_json_scripts_are_not_checked() -> None:
    html = (
        "<!doctype html><html><body>"
        "<script src='x.js'>)))</script>"
        "<script type='application/json'>{\"a\": [)</script>"
        "</body></html>"
    )
    assert generate_ui(html, settings=SETTINGS)["success"] is True
