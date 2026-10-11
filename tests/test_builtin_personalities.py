"""Built-in personality seeding: the Kevin row and the invariants it must keep.

Kevin is the one built-in whose natural register fights the install's
screen-reader rules (rocket emoji, em dashes), so the body is linted here
rather than trusted: pure ASCII, bounded size, wrapped in the persona tags
the other built-ins use, and served behind the history-reset directive.
"""
from __future__ import annotations

import app as app_module


def _builtins() -> dict[str, dict]:
    return {
        row["name"]: row
        for row in app_module._list_personalities(None)
        if row["is_builtin"]
    }


def test_kevin_is_seeded_as_a_builtin() -> None:
    kevin = _builtins()["Kevin"]
    assert kevin["is_builtin"] and not kevin["is_owned"]
    assert kevin["system_prompt"] == app_module._BUILTIN_KEVIN_PROMPT
    assert kevin["description"]
    assert len(kevin["description"]) <= app_module._PERSONALITY_DESC_MAX


def test_kevin_prompt_is_screen_reader_safe_and_cloneable() -> None:
    body = app_module._BUILTIN_KEVIN_PROMPT
    assert body.startswith('<persona name="Kevin">')
    assert body.rstrip().endswith("</persona>")
    # Pure ASCII rules out emoji, em dashes and every other decorative
    # character the tech-bro register reaches for by reflex.
    assert body.isascii()
    assert not any(line != line.rstrip() for line in body.splitlines())
    # User-owned rows are capped on POST/PATCH; a built-in over the cap
    # can't be cloned-then-edited, which is the documented customisation path.
    assert len(body) <= app_module._PERSONALITY_PROMPT_MAX


def test_kevin_append_carries_the_history_reset_directive() -> None:
    kevin = _builtins()["Kevin"]
    append = app_module._persona_body_with_directive(kevin)
    assert append.startswith(app_module.PERSONA_HISTORY_RESET_DIRECTIVE)
    assert append.endswith(app_module._BUILTIN_KEVIN_PROMPT)


def test_no_persona_stays_the_fresh_install_default() -> None:
    rows = app_module._list_personalities(None)
    builtin_ids = sorted(row["id"] for row in rows if row["is_builtin"])
    default = app_module._default_personality_id()
    assert default == builtin_ids[0]
    assert next(row for row in rows if row["id"] == default)["name"] == "No persona"
