"""The roundtable thread browser's list endpoint: filters before the limit."""
from __future__ import annotations

import app as app_module


def _create(topic: str) -> int:
    return app_module.roundtable_core.roundtable_create(topic=topic)["thread_id"]


def _listed(client, **params) -> list[int]:
    response = client.get("/api/roundtable/threads", params=params)
    assert response.status_code == 200
    return [t["thread_id"] for t in response.json()["threads"]]


def _project_key() -> str:
    return app_module._sanitize_project_key(app_module.DEFAULT_CWD)


def test_a_project_filter_finds_threads_older_than_the_limit(client):
    bound = _create("bound to the project, then buried")
    app_module._roundtable_set_project(bound, _project_key(), "anonymous")
    _create("newer thread nobody bound")
    _create("another newer thread nobody bound")

    assert bound in _listed(client, project=_project_key(), limit=2)


def test_the_unbound_filter_finds_threads_older_than_the_limit(client):
    unbound = _create("started over MCP, never bound")
    for topic in ("newer bound thread", "another newer bound thread"):
        app_module._roundtable_set_project(_create(topic), _project_key(), "anonymous")

    assert unbound in _listed(client, project="__unbound__", limit=2)


def test_another_users_threads_do_not_use_up_the_limit(client):
    mine = _create("mine, older")
    app_module._roundtable_set_project(mine, _project_key(), "anonymous")
    for topic in ("someone else's newer thread", "someone else's newest thread"):
        app_module._roundtable_set_project(_create(topic), _project_key(), "someone-else")

    assert _listed(client, limit=1) == [mine]


def test_the_unfiltered_list_still_returns_the_newest_first_up_to_the_limit(client):
    older = _create("older unfiltered thread")
    newer = _create("newer unfiltered thread")

    listed = _listed(client, limit=2)
    assert listed == [newer, older]
