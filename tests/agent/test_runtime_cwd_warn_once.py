"""4handy local patch: a missing configured cwd warns once per value, then logs at debug.

Container-configured bots (terminal.cwd = /workspace, checked on the host) logged
``TERMINAL_CWD does not exist: /workspace`` about every 2 s. The resolution result is unchanged.
"""

import logging

import pytest

from agent import runtime_cwd


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(runtime_cwd, "_WARNED_MISSING", set())


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "does not exist" in r.getMessage()]


def test_missing_terminal_cwd_warns_once_and_keeps_returning_none(monkeypatch, tmp_path, caplog):
    missing = str(tmp_path / "workspace")
    monkeypatch.setenv("TERMINAL_CWD", missing)
    monkeypatch.setattr(runtime_cwd, "scope_terminal_cwd", lambda: missing)
    caplog.set_level(logging.DEBUG, logger="agent.runtime_cwd")
    for _ in range(5):
        assert runtime_cwd.resolve_context_cwd() is None
        assert runtime_cwd.resolve_agent_cwd() == runtime_cwd.Path.cwd()
    warns = _warnings(caplog)
    assert len(warns) == 1 and missing in warns[0].getMessage()
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and missing in r.getMessage()]
    assert len(debugs) == 9


def test_each_distinct_value_or_label_warns_once(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="agent.runtime_cwd")
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    for _ in range(3):
        assert runtime_cwd._existing_dir(a, "TERMINAL_CWD") is None
        assert runtime_cwd._existing_dir(b, "TERMINAL_CWD") is None
        assert runtime_cwd._existing_dir(a, "configured working directory") is None
    assert len(_warnings(caplog)) == 3


def test_existing_dir_is_returned_and_never_warns(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="agent.runtime_cwd")
    assert runtime_cwd._existing_dir(str(tmp_path), "TERMINAL_CWD") == tmp_path
    assert caplog.records == []


def test_remembered_values_are_bounded(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(runtime_cwd, "_WARNED_MISSING_MAX", 2)
    caplog.set_level(logging.WARNING, logger="agent.runtime_cwd")
    for i in range(4):
        runtime_cwd._existing_dir(str(tmp_path / f"m{i}"), "TERMINAL_CWD")
    assert len(runtime_cwd._WARNED_MISSING) == 2
    runtime_cwd._existing_dir(str(tmp_path / "m3"), "TERMINAL_CWD")  # beyond the cap: still warns
    assert len(_warnings(caplog)) == 5
