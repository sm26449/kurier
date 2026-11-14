"""Status page: served with a strict CSP, assets present, no HTML injection paths."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

UI = Path(__file__).parent.parent / "src" / "ui"


def client():
    from src.main import app
    return TestClient(app)  # no lifespan: static routes only


def test_page_served_with_strict_csp():
    r = client().get("/ui")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp


def test_assets_served():
    c = client()
    assert c.get("/ui/assets/app.js").status_code == 200
    assert c.get("/ui/assets/app.css").status_code == 200


def test_no_inline_script_or_style_in_html():
    html = (UI / "index.html").read_text()
    assert "<script>" not in html and "style=" not in html
    assert not re.search(r"<style", html)


def test_untrusted_text_is_never_rendered_as_html():
    js = (UI / "app.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in js, sink
    # Inline styles would be blocked by the CSP (style-src 'self').
    assert not re.search(r"style:\s*`|\.style\.|setAttribute\(\s*[\"']style", js)


def test_no_external_resources():
    for f in ("index.html", "app.css", "app.js"):
        # The SVG namespace is an identifier, not a resource that gets loaded.
        text = (UI / f).read_text().replace("http://www.w3.org/2000/svg", "")
        assert "http://" not in text and "https://" not in text, f


def test_key_cleanup_handles_pasted_env_lines():
    js = (UI / "app.js").read_text()
    assert "function cleanKey" in js and "setKey(cleanKey(" in js
