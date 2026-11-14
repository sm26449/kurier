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
    for f in ("index.html", "app.css", "app.js", "i18n.js"):
        # The SVG namespace is an identifier, not a resource that gets loaded.
        text = (UI / f).read_text().replace("http://www.w3.org/2000/svg", "")
        assert "http://" not in text and "https://" not in text, f


def test_key_cleanup_handles_pasted_env_lines():
    js = (UI / "app.js").read_text()
    assert "function cleanKey" in js and "setKey(cleanKey(" in js


# ── Translations ──────────────────────────────────────────────

def _lang_keys(lang: str) -> set[str]:
    src = (UI / "i18n.js").read_text()
    start = src.index(f"  {lang}: {{")
    end = src.index("\n  },", start)
    return set(re.findall(r'^\s{4}"([\w.]+)":', src[start:end], re.M))


def test_both_languages_have_the_same_keys():
    en, ro = _lang_keys("en"), _lang_keys("ro")
    assert len(en) > 100
    assert en - ro == set(), f"missing in ro: {sorted(en - ro)}"
    assert ro - en == set(), f"missing in en: {sorted(ro - en)}"


def test_every_used_key_exists():
    keys = _lang_keys("en")
    js = (UI / "app.js").read_text()
    used = set(re.findall(r'\bt\("([\w.]+)"', js))
    for idle, armed in re.findall(r'confirmable\([^,]+, "([\w.]+)", "([\w.]+)"', js):
        used.update((idle, armed))
    html = (UI / "index.html").read_text()
    used |= set(re.findall(r'data-i18n(?:-aria)?="([\w.]+)"', html))
    assert used and used - keys == set(), f"unknown keys: {sorted(used - keys)}"


def test_i18n_script_is_served_and_safe():
    c = client()
    assert c.get("/ui/assets/i18n.js").status_code == 200
    js = (UI / "i18n.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in js
    html = (UI / "index.html").read_text()
    assert html.index("i18n.js") < html.index("app.js")  # t() defined before use
