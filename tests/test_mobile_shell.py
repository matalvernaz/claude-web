"""Mobile shell: header split into bar + sheet, and nothing lost on the way.

The header restructure is the risky part of the mobile work — it re-parents
about twenty controls. The inventory test below is the guard: it fails if a
control stops being rendered, which is the one regression that would otherwise
show up only as "the model picker is gone on my phone".
"""
from __future__ import annotations

import re
from pathlib import Path

import app as app_module

ROOT = Path(app_module.__file__).parent
INDEX = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
STYLE = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

# Every control the header carried before it was split into a bar and a sheet.
HEADER_CONTROL_IDS = [
    "perm-waiting",
    "toggle-sessions",
    "new-chat",
    "provider-select",
    "model-select",
    "permission-mode-select",
    "effort-select",
    "account-select",
    "personality-select",
    "personality-apply-current",
    "project-select",
    "sound-toggle",
    "sound-away-toggle",
    "header-cost",
    "show-usage",
]
HEADER_LINKS = ["/account", "/personalities", "/skills", "/mcp", "/roundtable", "/auth/logout"]

# The bar is what a phone shows without opening anything.
BAR_IDS = ["perm-waiting", "toggle-sessions", "new-chat", "header-menu-toggle"]


def _header_bar_block() -> str:
    m = re.search(r'<div class="header-bar">(.*?)</div>', INDEX, re.S)
    assert m, "header bar missing"
    return m.group(1)


def test_every_header_control_survived_the_split() -> None:
    for control_id in HEADER_CONTROL_IDS:
        assert f'id="{control_id}"' in INDEX, control_id
    for href in HEADER_LINKS:
        assert f'href="{href}"' in INDEX, href


def test_bar_holds_exactly_the_always_visible_controls() -> None:
    block = _header_bar_block()
    found = re.findall(r'id="([^"]+)"', block)
    assert found == BAR_IDS, found


def test_menu_toggle_announces_a_dialog() -> None:
    """It opens a modal sheet, so aria-haspopup is the honest attribute and
    aria-expanded would be a lie."""
    m = re.search(r'id="header-menu-toggle".*?>', INDEX, re.S)
    assert m, "menu toggle missing"
    assert 'aria-haspopup="dialog"' in m.group(0)
    assert "aria-expanded" not in m.group(0)


def test_both_sheets_are_labelled_dialogs_with_a_close_path() -> None:
    for dialog_id, body_id in (
        ("sessions-sheet", "sessions-sheet-body"),
        ("menu-sheet", "menu-sheet-body"),
    ):
        m = re.search(rf'<dialog id="{dialog_id}".*?</dialog>', INDEX, re.S)
        assert m, dialog_id
        block = m.group(0)
        assert "aria-labelledby=" in block, dialog_id
        assert f'id="{body_id}"' in block, body_id
        # method="dialog" is what makes Done close without any JS.
        assert 'method="dialog"' in block, dialog_id


def test_sheet_bodies_start_empty() -> None:
    """The live nodes are moved in at open time; a copy baked into the template
    would duplicate ids."""
    for body_id in ("sessions-sheet-body", "menu-sheet-body"):
        assert f'<div class="sheet-body" id="{body_id}"></div>' in INDEX, body_id


def test_moved_nodes_have_a_visible_rule_inside_their_sheet() -> None:
    """The phone rules hide both nodes in their inline position. Without an
    id-scoped override they stay hidden after being moved into the sheet, which
    reads as an empty sheet with no error anywhere."""
    assert "#menu-sheet .header-controls" in STYLE
    assert "#sessions-sheet nav#sessions-panel" in STYLE


def test_phone_breakpoint_hides_the_inline_controls() -> None:
    """display:none, not a visual hide: a screen reader must not find phantom
    controls outside the sheet."""
    assert re.search(r"\.header-controls \{ display: none; \}", STYLE)


def test_sheet_controller_moves_nodes_rather_than_cloning() -> None:
    assert "makeSheet(" in APP_JS
    assert "sessions-sheet-body" in APP_JS
    assert "menu-sheet-body" in APP_JS
    assert "cloneNode" not in APP_JS.split("makeSheet(")[1][:2000]
