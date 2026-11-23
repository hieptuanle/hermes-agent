"""4handy DELIVER-THREAD (2026-11-23): an explicit ``mattermost:<origin chat>`` deliver target keeps the
job's origin thread when the profile opts in, like upstream does for Slack.

A Mattermost job created inside a thread whose ``deliver`` names the same channel without a thread
segment posted at the channel root every run (kyduyenbot 5d8e12712fb6; 18 active jobs fleet-wide).
``cron.origin_thread_reattach_platforms`` lists the platforms that re-attach; Slack always does. The
"delivery target lost it" warning now fires only for the origin chat: a different chat never carries
the origin thread.
"""

from __future__ import annotations

import logging

import pytest

from cron import scheduler_delivery as sd
from cron.scheduler_delivery import _resolve_single_delivery_target

CHAN = "xac1spygffgpfrwi8ne7bssznw"
ROOT = "ps7qmamezjr43cw8bfqb9pcbbe"
ORIGIN = {"platform": "mattermost", "chat_id": CHAN, "thread_id": ROOT, "user_id": "u1"}


@pytest.fixture
def cron_cfg(monkeypatch):
    """Set the ``cron:`` config section the resolver reads; plain targets resolve as written."""
    state: dict = {}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"cron": dict(state)})
    monkeypatch.setattr("tools.send_message_tool.prepare_send_message_platforms", lambda: None)

    def _resolve(platform, rest, **kw):
        parts = rest.split(":")
        return parts[0], (parts[1] if len(parts) > 1 and parts[1] else None), None

    monkeypatch.setattr("tools.send_message_tool.resolve_send_target", _resolve)
    return state


def _resolve(deliver: str, origin: dict = ORIGIN):
    return _resolve_single_delivery_target({"id": "j", "origin": dict(origin)}, deliver)


def test_default_keeps_upstream_behaviour_for_mattermost(cron_cfg):
    assert _resolve(f"mattermost:{CHAN}")["thread_id"] is None


@pytest.mark.parametrize("value", [["slack", "mattermost"], ["mattermost"], "slack,mattermost", ["Mattermost"]])
def test_opted_in_mattermost_same_chat_reattaches_origin_thread(cron_cfg, value):
    cron_cfg["origin_thread_reattach_platforms"] = value
    target = _resolve(f"mattermost:{CHAN}")
    assert (target["chat_id"], target["thread_id"]) == (CHAN, ROOT)
    assert target["_resolved_from"] == "explicit"


def test_opted_in_other_chat_does_not_inherit(cron_cfg):
    cron_cfg["origin_thread_reattach_platforms"] = ["mattermost"]
    assert _resolve("mattermost:otherchannel00000000000000")["thread_id"] is None


def test_explicit_thread_segment_still_wins(cron_cfg):
    cron_cfg["origin_thread_reattach_platforms"] = ["mattermost"]
    assert _resolve(f"mattermost:{CHAN}:newroot0000000000000000000")["thread_id"] == "newroot0000000000000000000"


def test_origin_without_thread_posts_at_root(cron_cfg):
    cron_cfg["origin_thread_reattach_platforms"] = ["mattermost"]
    origin = {**ORIGIN, "thread_id": None}
    assert _resolve(f"mattermost:{CHAN}", origin)["thread_id"] is None


@pytest.mark.parametrize("value", [["mattermost"], [], None, "", 42])
def test_slack_always_reattaches(cron_cfg, value):
    cron_cfg["origin_thread_reattach_platforms"] = value
    origin = {"platform": "slack", "chat_id": "C0B3KEP3SD6", "thread_id": "1778485067.844139"}
    assert _resolve("slack:C0B3KEP3SD6", origin)["thread_id"] == "1778485067.844139"


def test_unreadable_config_falls_back_to_upstream(monkeypatch, cron_cfg):
    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config", _boom)
    assert _resolve(f"mattermost:{CHAN}")["thread_id"] is None


def test_deliver_origin_is_unchanged(cron_cfg):
    assert _resolve("origin")["thread_id"] == ROOT


# --- the "lost it" warning ------------------------------------------------------------------------


def _prepare(deliver_to: dict):
    from gateway.config import GatewayConfig

    return sd._prepare_target_delivery(
        {"id": "job-1", "origin": dict(ORIGIN)}, deliver_to, adapters={}, loop=None,
        config=GatewayConfig(), notify_delivery=True, mirror_enabled=False, mirror_text="",
        delivery_errors=[])


def _lost_lines(caplog):
    return [r for r in caplog.records if "delivery target lost it" in r.getMessage() and r.levelno >= logging.WARNING]


def test_warning_kept_for_the_origin_chat_without_thread(cron_cfg, caplog):
    with caplog.at_level(logging.DEBUG):
        try:
            _prepare({"platform": "mattermost", "chat_id": CHAN, "thread_id": None})
        except Exception:
            pass  # only the prologue's log line matters here
    assert len(_lost_lines(caplog)) == 1


def test_no_warning_for_a_different_chat(cron_cfg, caplog):
    with caplog.at_level(logging.DEBUG):
        try:
            _prepare({"platform": "mattermost", "chat_id": "otherchannel00000000000000", "thread_id": None})
        except Exception:
            pass
    assert _lost_lines(caplog) == []
    assert any("not the origin chat" in r.getMessage() for r in caplog.records)


def test_no_warning_once_the_thread_is_reattached(cron_cfg, caplog):
    cron_cfg["origin_thread_reattach_platforms"] = ["mattermost"]
    target = _resolve(f"mattermost:{CHAN}")
    with caplog.at_level(logging.DEBUG):
        try:
            _prepare(target)
        except Exception:
            pass
    assert _lost_lines(caplog) == []
