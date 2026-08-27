"""Home-screen / PWA surface: manifest, icons, and the head metadata.

These assertions look pedantic but each one maps to a silent failure mode: a
wrong scope or an auth-gated manifest produces an installed icon that opens in
the browser with nothing in any log to explain why.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module

STATIC_DIR = Path(app_module.__file__).parent / "static"

TEMPLATES = [
    "index.html",
    "account.html",
    "mcp.html",
    "personalities.html",
    "roundtable.html",
    "setup.html",
    "skills.html",
]


def r_json(client: TestClient) -> dict:
    return json.loads(client.get("/manifest.webmanifest").text)


def test_manifest_served_unauthenticated(client: TestClient) -> None:
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")


def test_manifest_scope_and_start_url_are_root(client: TestClient) -> None:
    """Anything narrower than "/" makes in-app navigation break out of the
    standalone window and back into the browser."""
    data = r_json(client)
    assert data["scope"] == "/"
    assert data["start_url"] == "/"
    assert data["display"] == "standalone"


def test_manifest_short_name_fits_a_home_screen_label(client: TestClient) -> None:
    data = r_json(client)
    assert data["short_name"] == "Claude"
    assert len(data["short_name"]) <= app_module.HOME_SCREEN_LABEL_MAX


def test_manifest_icons_exist_on_disk(client: TestClient) -> None:
    data = r_json(client)
    sizes = {icon["sizes"] for icon in data["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(icon.get("purpose") == "maskable" for icon in data["icons"])
    for icon in data["icons"]:
        name = icon["src"].removeprefix("/static/").split("?", 1)[0]
        assert (STATIC_DIR / name).is_file(), f"missing icon {name}"


def test_apple_touch_icon_exists() -> None:
    assert (STATIC_DIR / "apple-touch-icon.png").is_file()


def test_manifest_theme_matches_stylesheet_background() -> None:
    """theme_color paints the standalone status bar and splash screen; if it
    drifts from --bg the app flashes the wrong colour on every launch."""
    css = (STATIC_DIR / "style.css").read_text()
    assert f"--bg: {app_module.MANIFEST_BG_COLOR}" in css


def test_short_name_derivation() -> None:
    assert app_module._manifest_short_name("Claude — homelab") == "Claude"
    assert app_module._manifest_short_name("Claude - homelab") == "Claude"
    assert app_module._manifest_short_name("Team: Claude") == "Team"
    assert app_module._manifest_short_name("Averyverylongproductname") == "Averyverylon"
    assert app_module._manifest_short_name("—") == "Claude"


def test_every_template_carries_the_pwa_head() -> None:
    """iOS captures these tags from whichever page was showing when the site
    was added to the home screen, so one missing page is one broken icon."""
    template_dir = Path(app_module.__file__).parent / "templates"
    for name in TEMPLATES:
        text = (template_dir / name).read_text()
        assert '{% include "_pwa_head.html" %}' in text, name
        assert "viewport-fit=cover" in text, name


def test_index_marks_the_app_shell() -> None:
    """standalone.js only pins the viewport height on the chat page; the marker
    is how it tells."""
    template_dir = Path(app_module.__file__).parent / "templates"
    assert 'class="app-shell"' in (template_dir / "index.html").read_text()
