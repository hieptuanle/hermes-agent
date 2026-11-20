"""4handy local patch: per-job ``enabled_toolsets`` follow a left-core migration's ``replaces_toolsets``.

The camofox migration turned core ``browser`` off (``agent.disabled_toolsets``) and moved saved platform
lists to the plugin's ``camofox`` toolset, but left per-job cron lists alone: a job listing ``browser``
lost its browser tools. The cron resolver now adds the replacement toolsets for such a job when the
home ran the conversion and the replacement is on there.
"""

from pathlib import Path

import pytest

import cron.scheduler as s
from hermes_cli.config_effective import load_user_config_effective
from hermes_cli.left_core_migration import LEFT_CORE, LeftCoreFeature

MIGRATED = """\
_left_core_scoped:
- camofox
_left_core_installed:
- camofox
agent:
  disabled_toolsets:
  - browser
plugins:
  enabled:
  - camofox
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    return h


def _cfg(home: Path, text: str) -> dict:
    (home / "config.yaml").write_text(text, encoding="utf-8")
    return load_user_config_effective(home / "config.yaml")


def _enabled(job_toolsets, cfg) -> list[str]:
    return s._resolve_cron_enabled_toolsets({"enabled_toolsets": job_toolsets}, cfg)


def test_camofox_is_a_replacing_feature():
    camofox = next(f for f in LEFT_CORE if f.plugin == "camofox")
    assert camofox.replaces_toolsets == ("browser",) and "camofox" in camofox.toolsets


def test_migrated_home_adds_replacement(home):
    cfg = _cfg(home, MIGRATED)
    enabled = _enabled(["terminal", "browser", "file"], cfg)
    assert "camofox" in enabled
    assert enabled[:3] == ["terminal", "browser", "file"]  # job's own order kept, replacement appended
    assert "browser" in s._resolve_cron_disabled_toolsets(cfg)  # core browser stays off


def test_job_already_listing_replacement_unchanged(home):
    cfg = _cfg(home, MIGRATED)
    enabled = _enabled(["browser", "camofox", "terminal"], cfg)
    assert enabled.count("camofox") == 1
    assert enabled[:3] == ["browser", "camofox", "terminal"]


def test_job_without_replaced_toolset_unchanged(home):
    cfg = _cfg(home, MIGRATED)
    assert "camofox" not in _enabled(["terminal", "file"], cfg)


def test_home_without_migration_unchanged(home):
    cfg = _cfg(home, "agent:\n  disabled_toolsets: []\n")
    assert "camofox" not in _enabled(["terminal", "browser"], cfg)


def test_replacement_disabled_in_home_not_added(home):
    cfg = _cfg(home, MIGRATED.replace("  - browser\n", "  - browser\n  - camofox\n"))
    assert "camofox" not in _enabled(["browser"], cfg)


def test_replacement_off_for_cron_platform_not_added(home):
    cfg = _cfg(home, MIGRATED + "known_plugin_toolsets:\n  cron:\n  - camofox\n")
    assert "camofox" not in _enabled(["browser"], cfg)


def test_disabled_toolsets_as_json_string(home):
    cfg = _cfg(home, MIGRATED.replace("  disabled_toolsets:\n  - browser\n",
                                      "  disabled_toolsets: '[\"browser\", \"camofox\"]'\n"))
    assert "camofox" not in _enabled(["browser"], cfg)


def test_explicit_empty_and_unset_unchanged(home):
    cfg = _cfg(home, MIGRATED)
    assert _enabled([], cfg) == []
    # unset per-job list: platform resolution, untouched by the patch
    platform = s._resolve_cron_enabled_toolsets({}, cfg)
    assert isinstance(platform, list)


def test_generic_over_replaces_toolsets(home, monkeypatch):
    fake = LeftCoreFeature(plugin="newsearch", label="New search", in_use=lambda *a, **k: False,
                           unchanged="", toolsets=("newsearch",), replaces_toolsets=("web",))
    monkeypatch.setattr("hermes_cli.left_core_migration.LEFT_CORE", (*LEFT_CORE, fake))
    cfg = _cfg(home, "_left_core_scoped:\n- newsearch\nagent:\n  disabled_toolsets:\n  - web\n")
    enabled = _enabled(["web", "terminal"], cfg)
    assert "newsearch" in enabled and "camofox" not in enabled
    assert "newsearch" not in _enabled(["browser"], cfg)


def test_mcp_merge_still_applies(home, monkeypatch):
    monkeypatch.setattr("hermes_cli.tools_config.enabled_mcp_server_names", lambda cfg: {"erp"})
    cfg = _cfg(home, MIGRATED)
    enabled = _enabled(["browser"], cfg)
    assert "camofox" in enabled and any("erp" in t for t in enabled), enabled
