"""4handy local patch: the per-profile kanban board fence (``kanban.board_fence``), rules 0-3.

Rules 4-6 (card admission, ``/kanban`` in chat, the worker client line) are tested in
``tests/gateway/test_kanban_board_fence_gateway.py``.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

BOX, OTHER = "box-hiep", "box-b"


# --- Fixtures and helpers ---------------------------------------------------------------------

@pytest.fixture
def root(tmp_path, monkeypatch):
    """A scratch Hermes root with boards ``box-hiep`` and ``box-b`` and no kanban env pins."""
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_HOME", "HERMES_KANBAN_CLAIM_LOCK", "HERMES_PROFILE", "HERMES_PROFILE_NAME",
                "HERMES_SESSION_ID", "HERMES_TENANT", DELEGATED_CHILD_ENV_MARKER):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    kb.create_board(BOX)
    kb.create_board(OTHER)
    return home


def _profile(root: Path, name: str, kanban: dict | None = None) -> Path:
    home = root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps({"kanban": kanban} if kanban is not None else {}) + "\n",
                                      encoding="utf-8")
    return home


def _box(root: Path) -> dict[str, Path]:
    """hiepbot (orchestrator) and two specialists fenced to box-hiep, plus outsiders."""
    return {
        "hiepbot": _profile(root, "hiepbot", {"board_fence": BOX}),
        "hiep-tracuu": _profile(root, "hiep-tracuu", {"board_fence": BOX}),
        "hiep-review": _profile(root, "hiep-review", {"board_fence": BOX}),
        "ketoanbot": _profile(root, "ketoanbot"),
        "anybot": _profile(root, "anybot", {"board_fence": "*"}),
        "boxbbot": _profile(root, "boxbbot", {"board_fence": OTHER}),
    }


@contextmanager
def _as(home: Path):
    """Run as *home*'s profile the way a multiplexed gateway turn does (context-local override)."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _db(root: Path, board: str) -> Path:
    return root / "kanban.db" if board == "default" else root / "kanban" / "boards" / board / "kanban.db"


def _state(root: Path, board: str) -> tuple:
    """Task rows and event count of one board, read by path (no board resolution involved)."""
    with sqlite3.connect(_db(root, board).as_uri() + "?mode=ro", uri=True) as conn:
        tasks = conn.execute("SELECT id, status, assignee, current_run_id FROM tasks ORDER BY id").fetchall()
        events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    return tasks, events


def _all_states(root: Path) -> dict:
    return {board: _state(root, board) for board in ("default", BOX, OTHER)}


def _seed(root: Path, board: str, assignee: str, *, claim: bool = False):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    conn = connect(db_path=_db(root, board))
    try:
        tid = kb.create_task(conn, title=f"seed {board}", assignee=assignee)
        if claim:
            kb.recompute_ready(conn)
            kb.claim_task(conn, tid)
        return tid, kb._current_run_id(conn, tid)
    finally:
        conn.close()


def _tools():
    from tools import kanban_tools as kt
    return {name: handler for name, _schema, handler, _emoji in kt._TOOLS}


def _call(tool: str, args: dict) -> dict:
    return json.loads(_tools()[tool](args))


ALL_TOOLS = ("kanban_show", "kanban_list", "kanban_complete", "kanban_block", "kanban_schedule",
             "kanban_request_review", "kanban_request_changes", "kanban_heartbeat", "kanban_comment",
             "kanban_attach", "kanban_attach_url", "kanban_attachments", "kanban_create", "kanban_unblock",
             "kanban_link")


def test_tool_list_is_the_fifteen_this_patch_covers():
    assert tuple(_tools()) == ALL_TOOLS


# --- The setting ------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, ("unset",)), ("", ("unset",)), ("  ", ("unset",)), ("*", ("any",)),
    ("box-hiep", ("board", "box-hiep")), (" BOX-Hiep ", ("board", "box-hiep")),
    ("box hiep", ("error",)), ("../x", ("error",)), (5, ("error",)), (["box-hiep"], ("error",)),
])
def test_parse_fence(value, expected):
    from hermes_cli.kanban_board_fence import parse_fence
    fence = parse_fence(value)
    kind = ("error",) if fence.error else ("board", fence.board) if fence.board else ("any",) if fence.any else ("unset",)
    assert kind == expected


def test_defaults_declare_both_keys():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["kanban"]["board_fence"] is None
    assert DEFAULT_CONFIG["kanban"]["chat_command"] is False


# --- Rule 0: the kanban toolset without a setting ---------------------------------------------

@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_rule0_unset_named_profile_is_refused_and_writes_nothing(root, tool):
    homes = _box(root)
    _seed(root, "default", "ketoanbot")
    before = _all_states(root)
    with _as(homes["ketoanbot"]):
        out = _call(tool, {})  # the fence runs before each handler's own argument checks
    assert "kanban tools need kanban.board_fence on this profile" in out["error"]
    assert out["error"].startswith(f"{tool} refused:") and out["error"].endswith("Nothing changed.")
    assert _all_states(root) == before


def test_rule0_star_keeps_upstream_resolution(root):
    homes = _box(root)
    default_tid, _ = _seed(root, "default", "anybot")
    other_tid, _ = _seed(root, OTHER, "anybot")
    with _as(homes["anybot"]):
        assert [t["id"] for t in _call("kanban_list", {})["tasks"]] == [default_tid]
        assert [t["id"] for t in _call("kanban_list", {"board": OTHER})["tasks"]] == [other_tid]
        created = _call("kanban_create", {"title": "any", "assignee": "ketoanbot", "board": OTHER})
    assert created["ok"] is True  # "*" is not fenced: no rule 3 either


def test_rule0_default_profile_without_setting_keeps_upstream_resolution(root):
    default_tid, _ = _seed(root, "default", "ketoanbot")
    other_tid, _ = _seed(root, OTHER, "ketoanbot")
    assert [t["id"] for t in _call("kanban_list", {})["tasks"]] == [default_tid]
    assert [t["id"] for t in _call("kanban_list", {"board": OTHER})["tasks"]] == [other_tid]


# --- Rules 1 and 2: pin and refuse -----------------------------------------------------------

def test_rule1_no_board_means_the_fence_board_whatever_the_env_and_current_file_say(root, monkeypatch):
    from hermes_cli import kanban_db as kb
    homes = _box(root)
    box_tid, _ = _seed(root, BOX, "hiep-tracuu")
    _seed(root, OTHER, "boxbbot")
    _seed(root, "default", "ketoanbot")
    kb.set_current_board(OTHER)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", OTHER)
    with _as(homes["hiepbot"]):
        assert [t["id"] for t in _call("kanban_list", {})["tasks"]] == [box_tid]
        assert [t["id"] for t in _call("kanban_list", {"board": ""})["tasks"]] == [box_tid]
        created = _call("kanban_create", {"title": "lands in the box", "assignee": "hiep-tracuu"})
    assert created["ok"] is True
    assert created["task_id"] in [row[0] for row in _state(root, BOX)[0]]
    assert created["task_id"] not in [row[0] for row in _state(root, OTHER)[0]]


@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_rule2_other_board_refused_for_every_tool(root, tool, caplog):
    homes = _box(root)
    _seed(root, OTHER, "boxbbot", claim=True)
    before = _all_states(root)
    caplog.set_level(logging.WARNING, logger="tools.kanban_tools")
    with _as(homes["hiepbot"]):
        out = _call(tool, {"board": OTHER})
    assert out["error"] == (f"{tool} refused: this profile is fenced to board 'box-hiep'; 'box-b' is outside "
                            "the fence. Omit 'board' or pass 'box-hiep'. Nothing changed.")
    assert _all_states(root) == before
    fence_logs = [r for r in caplog.records if "kanban board fence" in r.getMessage()]
    assert len(fence_logs) == 1 and "hiepbot" in fence_logs[0].getMessage()


def test_rule2_board_name_is_normalised_before_the_compare(root):
    homes = _box(root)
    box_tid, _ = _seed(root, BOX, "hiep-tracuu")
    with _as(homes["hiepbot"]):
        assert [t["id"] for t in _call("kanban_list", {"board": " BOX-HIEP "})["tasks"]] == [box_tid]


def test_rule2_applies_to_delegate_children_and_cron_runs(root, monkeypatch):
    from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context
    homes = _box(root)
    with _as(homes["hiepbot"]):
        with delegated_child_context("child-session"):
            assert "outside the fence" in _call("kanban_show", {"task_id": "t_x", "board": OTHER})["error"]
        # A cron job fired in-process beside a dispatched worker: HERMES_KANBAN_TASK is present but
        # not owned by this execution, so the fence applies.
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_someone_else")
        with non_dispatcher_owned_context():
            assert "outside the fence" in _call("kanban_show", {"task_id": "t_x", "board": OTHER})["error"]


# --- Dispatched workers: the dispatcher's pin, unchanged ----------------------------------------

def _become_worker(monkeypatch, root: Path, home: Path, board: str, tid: str, run_id: int) -> None:
    """The env the dispatcher gives a worker: its profile home and the card's pins."""
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", home.name)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(_db(root, board)))


@pytest.mark.parametrize("profile,board", [("hiep-tracuu", BOX), ("ketoanbot", "default")])
def test_dispatched_worker_keeps_the_pin_and_gets_no_new_refusal(root, monkeypatch, profile, board):
    homes = _box(root)
    tid, run_id = _seed(root, board, profile, claim=True)
    _become_worker(monkeypatch, root, homes[profile], board, tid, run_id)
    elsewhere = OTHER if board == BOX else BOX
    out = _call("kanban_show", {"board": elsewhere})  # explicit board: ignored by the pin, as upstream
    assert "error" not in out, out
    assert out["task"]["id"] == tid
    assert _call("kanban_heartbeat", {"note": "alive"})["ok"] is True


def test_dispatched_worker_of_a_fenced_profile_is_still_bound_by_rule3(root, monkeypatch):
    homes = _box(root)
    tid, run_id = _seed(root, BOX, "hiep-tracuu", claim=True)
    _become_worker(monkeypatch, root, homes["hiep-tracuu"], BOX, tid, run_id)
    before = _all_states(root)
    out = _call("kanban_create", {"title": "escape", "assignee": "ketoanbot"})
    assert "assignee 'ketoanbot' is not in board 'box-hiep'" in out["error"]
    assert _all_states(root) == before
    assert _call("kanban_create", {"title": "inside", "assignee": "hiep-review"})["ok"] is True


# --- Rule 3: handing a card to another profile ------------------------------------------------

@pytest.mark.parametrize("assignee,why", [
    ("ketoanbot", "its kanban.board_fence is unset"),
    ("anybot", 'its kanban.board_fence is "*"'),
    ("boxbbot", "its kanban.board_fence is 'box-b'"),
    ("ghost", "profile 'ghost' is not installed"),
])
def test_rule3_create_refuses_assignees_outside_the_box(root, assignee, why):
    homes = _box(root)
    before = _all_states(root)
    with _as(homes["hiepbot"]):
        out = _call("kanban_create", {"title": "x", "assignee": assignee})
    assert f"assignee '{assignee}' is not in board 'box-hiep'" in out["error"] and why in out["error"]
    assert _all_states(root) == before


def test_rule3_create_accepts_a_box_member(root):
    homes = _box(root)
    with _as(homes["hiepbot"]):
        out = _call("kanban_create", {"title": "look it up", "assignee": "hiep-tracuu"})
    assert out["ok"] is True
    (row,) = [r for r in _state(root, BOX)[0] if r[0] == out["task_id"]]
    assert row[2] == "hiep-tracuu"


def test_rule3_create_refuses_triage_cards(root):
    homes = _box(root)
    before = _all_states(root)
    with _as(homes["hiepbot"]):
        out = _call("kanban_create", {"title": "x", "assignee": "hiep-tracuu", "triage": True})
    assert "triage cards are not allowed on a fenced board" in out["error"]
    assert _all_states(root) == before


def test_rule3_assignee_with_an_unparseable_config_is_refused(root):
    homes = _box(root)
    (homes["hiep-review"] / "config.yaml").write_text("kanban: [unclosed\n", encoding="utf-8")
    with _as(homes["hiepbot"]):
        out = _call("kanban_create", {"title": "x", "assignee": "hiep-review"})
    assert "assignee 'hiep-review' is not in board 'box-hiep'" in out["error"] and "cannot be read" in out["error"]


def test_rule3_request_review_refuses_a_reviewer_outside_the_box(root, monkeypatch):
    homes = _box(root)
    tid, run_id = _seed(root, BOX, "hiep-tracuu", claim=True)
    _become_worker(monkeypatch, root, homes["hiep-tracuu"], BOX, tid, run_id)
    before = _all_states(root)
    out = _call("kanban_request_review", {"summary": "done, tested", "reviewer": "ketoanbot"})
    assert "reviewer 'ketoanbot' is not in board 'box-hiep'" in out["error"]
    assert _all_states(root) == before  # still running, same assignee, no event


def test_rule3_request_review_accepts_a_box_reviewer_and_no_reviewer(root, monkeypatch):
    homes = _box(root)
    tid, run_id = _seed(root, BOX, "hiep-tracuu", claim=True)
    _become_worker(monkeypatch, root, homes["hiep-tracuu"], BOX, tid, run_id)
    assert _call("kanban_request_review", {"summary": "done, tested", "reviewer": "hiep-review"})["ok"] is True
    (row,) = [r for r in _state(root, BOX)[0] if r[0] == tid]
    assert row[1:3] == ("review", "hiep-review")


def test_rule3_request_review_without_reviewer_is_unchanged(root, monkeypatch):
    homes = _box(root)
    tid, run_id = _seed(root, BOX, "hiep-tracuu", claim=True)
    _become_worker(monkeypatch, root, homes["hiep-tracuu"], BOX, tid, run_id)
    assert _call("kanban_request_review", {"summary": "done, tested"})["ok"] is True
    (row,) = [r for r in _state(root, BOX)[0] if r[0] == tid]
    assert row[1:3] == ("review", "hiep-tracuu")


def test_schema_guard_every_parameter_that_names_a_profile_was_reviewed():
    """Rule 3 covers every tool parameter that hands a card to a profile. A new upstream parameter
    whose name or description names a profile fails here until someone decides whether rule 3
    covers it (spec B1a 2.3)."""
    from tools import kanban_tools as kt
    reviewed = {
        "kanban_create.assignee": "rule 3",
        "kanban_request_review.reviewer": "rule 3",
        "kanban_create.triage": "rule 3: refused for a fenced profile",
        "kanban_list.assignee": "read-only filter",
        "kanban_create.model": "names the assignee profile's model, not a profile",
        "kanban_create.provider": "names the assignee profile's provider, not a profile",
        "kanban_create.skills": "skills on the assignee's profile, not a profile",
        "kanban_complete.created_cards": "task ids this profile created, not a profile",
    }
    names = {"assignee", "reviewer", "profile", "owner", "implementer", "specifier", "worker_profile"}
    found = set()
    for name, schema, _handler, _emoji in kt._TOOLS:
        properties = schema["parameters"]["properties"]
        assert "board" in properties, name  # rules 1-2 rely on every tool taking `board`
        for prop, spec in properties.items():
            if prop in names or re.search(r"\bprofiles?\b", str(spec.get("description", "")), re.I):
                found.add(f"{name}.{prop}")
    assert found == set(reviewed)


# --- Bad settings ----------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_invalid_fence_errors_every_tool(root, tool):
    bad = _profile(root, "badbot", {"board_fence": "Box Hiep!"})
    before = _all_states(root)
    with _as(bad):
        out = _call(tool, {})
    assert "kanban.board_fence 'Box Hiep!' is invalid" in out["error"]
    assert _all_states(root) == before


def test_fence_on_a_missing_board_errors_and_creates_nothing(root):
    lost = _profile(root, "lostbot", {"board_fence": "box-missing"})
    with _as(lost):
        out = _call("kanban_list", {})
    assert "error" in out and "box-missing" in out["error"]
    assert not (root / "kanban" / "boards" / "box-missing").exists()


def test_broken_live_config_that_mentions_the_fence_fails_closed(root):
    home = _profile(root, "brokenbot", {"board_fence": BOX})
    with _as(home):
        assert "error" not in _call("kanban_list", {})  # the loader keeps this good copy as last-known-good
        (home / "config.yaml").write_text("kanban:\n  board_fence: [box-hiep\n", encoding="utf-8")
        out = _call("kanban_list", {})
    assert "cannot be read" in out["error"]


# --- The session's frozen config and the live file --------------------------------------------

def test_fence_added_after_a_session_froze_its_config_still_binds(root, monkeypatch):
    from hermes_cli import config as config_module
    homes = _box(root)
    monkeypatch.setattr(config_module, "load_config_readonly", lambda: {"kanban": {"board_fence": "*"}})
    with _as(homes["hiepbot"]):  # live file: box-hiep
        assert "outside the fence" in _call("kanban_list", {"board": OTHER})["error"]


def test_session_and_live_fence_on_different_boards_is_an_error(root, monkeypatch):
    from hermes_cli import config as config_module
    homes = _box(root)
    monkeypatch.setattr(config_module, "load_config_readonly", lambda: {"kanban": {"board_fence": OTHER}})
    with _as(homes["hiepbot"]):
        assert "differs between this session's config" in _call("kanban_list", {})["error"]


# --- Per profile, not per process (the multiplexed gateway) ------------------------------------

def test_two_homes_in_one_process_are_fenced_independently(root):
    import threading
    homes = _box(root)
    _seed(root, OTHER, "boxbbot")
    results = {}

    def run(name):
        with _as(homes[name]):
            for _ in range(5):
                results.setdefault(name, []).append(_call("kanban_list", {"board": OTHER}))

    threads = [threading.Thread(target=run, args=(n,)) for n in ("hiepbot", "anybot")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all("outside the fence" in out.get("error", "") for out in results["hiepbot"])
    assert all("error" not in out and len(out["tasks"]) == 1 for out in results["anybot"])
