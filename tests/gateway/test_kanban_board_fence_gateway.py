"""4handy local patch, board fence: rule 4 (a fenced profile's gateway runs cards from its own board
only), the worker client's one-line refusal, and rules 5-6 (``/kanban`` in chat). Rules 0-3 are in
``tests/tools/test_kanban_board_fence.py``."""
import asyncio
import json
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

BOX, OTHER = "box-hiep", "box-b"


# --- Rule 4: card admission -------------------------------------------------------------------

@pytest.fixture
def boards(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("hermes_cli.profiles.resolve_profile_env",
                        lambda name: str(tmp_path) if name == "default" else str(tmp_path / "profiles" / name))
    kb.create_board(BOX)
    kb.create_board(OTHER)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return tmp_path, workspace


def _owner(tmp_path: Path, name: str, config_text: str) -> Path:
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(config_text, encoding="utf-8")
    return home


def _claim(board: str, assignee: str, workspace: Path):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    with closing(connect(board=board)) as conn:
        tid = kb.create_task(conn, title=f"card on {board}", assignee=assignee, workspace_kind="dir",
                             workspace_path=str(workspace))
        kb.recompute_ready(conn)
        return kb.claim_task(conn, tid)


def _connection(home: Path, **authority):
    from gateway.session_contract import SessionActor
    actor = SessionActor("owner", str(home), frozenset({"session:create"}), "transport")
    return SimpleNamespace(authority=SimpleNamespace(profile_id=str(home), **authority), actor=actor,
                           native_owner=True)


def _params(board: str, task):
    return dict(board=board, task_id=task.id, run_id=task.current_run_id, claim_lock=task.claim_lock)


CONFIG = {"model": {"default": "m", "provider": "custom"}, "platform_toolsets": {"cli": ["terminal"]}}


@pytest.mark.parametrize("config_text,board,refusal", [
    ("kanban:\n  board_fence: box-hiep\n", OTHER, "board_fenced"),
    ("kanban:\n  board_fence: box-hiep\n", BOX, None),
    ("kanban:\n  board_fence: BOX-HIEP\n", BOX, None),
    ("{}\n", OTHER, None),  # unset: upstream behaviour
    ("kanban:\n  board_fence: '*'\n", OTHER, None),
    ("kanban:\n  board_fence: Box Hiep!\n", BOX, "board_fence_invalid"),
    ("kanban:\n  board_fence: [box-hiep\n", BOX, "board_fence_invalid"),  # unparseable, names the fence
    ("model: [unclosed\n", OTHER, None),  # unparseable, never fenced: upstream behaviour
])
def test_rule4_owner_admission(boards, config_text, board, refusal):
    from gateway.session_kanban import build_kanban_policy
    from hermes_state_runtime import RuntimeStoreError
    tmp_path, workspace = boards
    home = _owner(tmp_path, "hiep-tracuu", config_text)
    task = _claim(board, "hiep-tracuu", workspace)
    if refusal is None:
        policy, _secrets = build_kanban_policy(_connection(home), _params(board, task), CONFIG)
        assert json.loads(policy.kanban_json)["board"] == board
        return
    with pytest.raises(RuntimeStoreError) as raised:
        build_kanban_policy(_connection(home), _params(board, task), CONFIG)
    assert raised.value.reason == refusal and raised.value.field == "board"
    if refusal == "board_fenced":
        assert raised.value.detail == "card on board 'box-b'; profile 'hiep-tracuu' is fenced to 'box-hiep'"


def test_rule4_refusal_creates_no_session_and_no_admission_marker(boards, monkeypatch):
    from gateway.session_kanban import run_task
    from hermes_cli.kanban_db_connect import connect
    from hermes_state_runtime import RuntimeStoreError
    tmp_path, workspace = boards
    home = _owner(tmp_path, "hiep-tracuu", "kanban:\n  board_fence: box-hiep\n")
    task = _claim(OTHER, "hiep-tracuu", workspace)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: CONFIG)
    calls = []
    monkeypatch.setattr("gateway.session_local.create_local_session",
                        lambda *a, **k: calls.append("create_local_session"))

    async def submit(*a, **k):
        calls.append("submit")

    async def resume(*a, **k):
        calls.append("resume")

    db = SimpleNamespace(get_session=lambda sid: None, db_path=str(home / "state.db"))
    connection = _connection(home, db=db, submit=submit)
    connection.resume = resume
    with pytest.raises(RuntimeStoreError, match="board_fenced"):
        asyncio.run(run_task(connection, _params(OTHER, task)))
    assert calls == []
    with closing(connect(board=OTHER)) as conn:
        assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='owner_admitted'",
                            (task.id,)).fetchone()[0] == 0


# --- The worker client's refusal line --------------------------------------------------------

def _worker_env(monkeypatch, tmp_path):
    for key, value in (("HERMES_KANBAN_BOARD", OTHER), ("HERMES_KANBAN_TASK", "t_1"), ("HERMES_KANBAN_RUN_ID", "7"),
                       ("HERMES_KANBAN_CLAIM_LOCK", "lock"), ("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))):
        monkeypatch.setenv(key, value)


def test_worker_client_prints_the_owner_refusal_with_its_reason(tmp_path, monkeypatch, capsys):
    from hermes_cli import kanban_worker_client as client
    from hermes_cli.gateway_client import GatewayRPCError
    _worker_env(monkeypatch, tmp_path)
    detail = "card on board 'box-b'; profile 'hiep-tracuu' is fenced to 'box-hiep'"

    async def refuse(params, board_db):
        raise GatewayRPCError("board_fenced", data={"reason": "board_fenced", "field": "board", "detail": detail})

    monkeypatch.setattr(client, "run", refuse)
    assert client._submit() == 1
    assert capsys.readouterr().err.strip().splitlines()[-1] == f"Kanban owner refused: board_fenced (board: {detail})"


def test_worker_client_refusal_without_detail_reads_as_before(tmp_path, monkeypatch, capsys):
    from hermes_cli import kanban_worker_client as client
    from hermes_cli.gateway_client import GatewayRPCError
    _worker_env(monkeypatch, tmp_path)

    async def refuse(params, board_db):
        raise GatewayRPCError("stale_kanban_claim", data={"reason": "stale_kanban_claim"})

    monkeypatch.setattr(client, "run", refuse)
    assert client._submit() == 1
    assert capsys.readouterr().err.strip().splitlines()[-1] == "Kanban owner refused: stale_kanban_claim"


# --- Rules 5 and 6: /kanban in chat -----------------------------------------------------------

@pytest.fixture
def slash(tmp_path, monkeypatch):
    """A root home (the launch profile) and a recorder in place of the kanban CLI."""
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    calls = []
    monkeypatch.setattr("hermes_cli.kanban.run_slash", lambda text: calls.append(text) or "board output")
    return root, calls


def _config(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


def _runner(*, multiplex: bool = False, served=()):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner._owns_kanban_dispatcher_lock = lambda: True
    if multiplex:
        runner.config = SimpleNamespace(multiplex_profiles=True)
        runner._primary_profile_name = "default"
        runner._profile_configs = {name: object() for name in served}
    return runner


def _kanban(runner, *, profile=None, text="/kanban list"):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="c1", chat_type="dm", thread_id=None,
                             user_id="u1", profile=profile)
    return asyncio.run(GatewayRunner._handle_kanban_command(runner, SimpleNamespace(text=text, source=source)))


NOT_ENABLED = "/kanban is not enabled on this bot."
FENCED = "/kanban is off on this bot: it works only on board 'box-hiep'. Ask the bot instead."


@pytest.mark.parametrize("config_text,expected", [
    (None, NOT_ENABLED),  # no config.yaml: the default is off
    ("{}\n", NOT_ENABLED),
    ("kanban:\n  chat_command: 'yes'\n", NOT_ENABLED),  # only a real true opens it
    ("kanban:\n  chat_command: true\n", "board output"),
    ("kanban:\n  chat_command: true\n  board_fence: box-hiep\n", FENCED),  # rule 5 wins
    ("kanban:\n  chat_command: true\n  board_fence: Bad Slug\n", NOT_ENABLED),
    ("kanban: [unclosed\n", NOT_ENABLED),
])
def test_rules5_6_single_profile_gateway(slash, config_text, expected):
    root, calls = slash
    if config_text is not None:
        _config(root, config_text)
    assert _kanban(_runner()) == expected
    assert calls == (["list"] if expected == "board output" else [])


@pytest.mark.parametrize("served_config,loaded,expected", [
    # hiepbot: fenced, served by the default gateway whose launch profile allows /kanban
    ("kanban:\n  chat_command: true\n  board_fence: box-hiep\n", True, FENCED),
    ("{}\n", True, NOT_ENABLED),  # a served bot without chat_command
    ("kanban:\n  chat_command: true\n", False, NOT_ENABLED),  # served profile's config not loaded here
    ("kanban: [unclosed\n", True, NOT_ENABLED),  # served profile's config unreadable
    ("kanban:\n  chat_command: true\n", True, "board output"),  # a served bot that opted in
])
def test_rules5_6_multiplexed_gateway_reads_the_bot_owning_profile(slash, served_config, loaded, expected):
    root, calls = slash
    _config(root, "kanban:\n  chat_command: true\n")  # the launch (default) profile opted in
    _config(root / "profiles" / "hiepbot", served_config)
    runner = _runner(multiplex=True, served=("hiepbot",) if loaded else ())
    assert _kanban(runner, profile="hiepbot") == expected
    assert calls == (["list"] if expected == "board output" else [])


@pytest.mark.parametrize("profile", [None, "default"])
def test_rules5_6_multiplexed_gateway_launch_profiles_own_bot(slash, profile):
    root, calls = slash
    _config(root, "kanban:\n  chat_command: true\n")
    _config(root / "profiles" / "hiepbot", "kanban:\n  board_fence: box-hiep\n")
    assert _kanban(_runner(multiplex=True, served=("hiepbot",)), profile=profile) == "board output"
    assert calls == ["list"]
