"""4handy local patch: skill paths handed to the model are where the terminal backend sees them.

With ``terminal.backend: docker`` the skills roots are mounted at ``/root/.hermes/skills`` and
``/root/.hermes/external_skills/<i>``, but ``skill_view``'s ``skill_dir``, ``${HERMES_SKILL_DIR}``
and the ``[Skill directory: ...]`` footer gave the gateway-host path
(``/home/<user>/.hermes/profiles/<p>/skills/...``). The model ran ``python <host path>/scripts/x.py``
inside the sandbox and failed (minhvubot cron ad46536e64cc, 2026-11-16 08:00).
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import tools.credential_files as cf
from agent.skill_preprocessing import preprocess_skill_content
from tools.credential_files import map_skill_path_to_container, to_agent_visible_skill_path


@pytest.fixture(autouse=True)
def _clean_state():
    cf.clear_credential_files()
    cf._config_files = {}
    yield
    cf.clear_credential_files()
    cf._config_files = {}


def _skill(root: Path, rel: str, body: str = "Do the thing.") -> Path:
    skill_dir = root / rel
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    name = skill_dir.name
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Description for {name}.\n---\n\n# {name}\n\n{body}\n", encoding="utf-8")
    (skill_dir / "scripts" / "tool.py").write_text("print('ok')\n", encoding="utf-8")
    return skill_dir


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A profile home with a local skill and one external skills dir."""
    hermes_home = tmp_path / ".hermes"
    local = hermes_home / "skills"
    shared = tmp_path / "shared-skills"
    _skill(local, "productivity/google-workspace",
           body="Run: python ${HERMES_SKILL_DIR}/scripts/tool.py")
    _skill(shared, "productivity/erp-data-retrieval")
    (hermes_home / "config.yaml").write_text(f"skills:\n  external_dirs:\n    - {shared}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    with patch("tools.skills_tool.SKILLS_DIR", local):
        yield hermes_home, local, shared


class TestMapping:
    def test_docker_local_skill_maps_under_root_hermes(self, home, monkeypatch):
        _home, local, _shared = home
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        skill_dir = local / "productivity" / "google-workspace"
        assert to_agent_visible_skill_path(str(skill_dir)) == "/root/.hermes/skills/productivity/google-workspace"
        assert (to_agent_visible_skill_path(str(skill_dir / "scripts" / "tool.py"))
                == "/root/.hermes/skills/productivity/google-workspace/scripts/tool.py")
        assert to_agent_visible_skill_path(str(local)) == "/root/.hermes/skills"

    def test_docker_external_skill_maps_to_its_mount_index(self, home, monkeypatch):
        _home, _local, shared = home
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        assert (to_agent_visible_skill_path(str(shared / "productivity" / "erp-data-retrieval"))
                == "/root/.hermes/external_skills/0/productivity/erp-data-retrieval")

    def test_ssh_maps_under_tilde_hermes(self, home, monkeypatch):
        _home, local, _shared = home
        monkeypatch.setenv("TERMINAL_ENV", "ssh")
        assert (to_agent_visible_skill_path(str(local / "productivity" / "google-workspace"))
                == "~/.hermes/skills/productivity/google-workspace")

    @pytest.mark.parametrize("backend", ["local", "", "singularity"])
    def test_untranslated_backends_keep_host_path(self, home, monkeypatch, backend):
        _home, local, _shared = home
        monkeypatch.setenv("TERMINAL_ENV", backend)
        skill_dir = str(local / "productivity" / "google-workspace")
        assert to_agent_visible_skill_path(skill_dir) == skill_dir

    def test_path_outside_skill_roots_passes_through(self, home, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        assert to_agent_visible_skill_path("/opt/plugins/foo/skills/bar") == "/opt/plugins/foo/skills/bar"

    @pytest.mark.require_symlinks
    def test_symlinked_root_maps_without_building_the_safe_copy(self, home, monkeypatch, tmp_path):
        """Roots served through the sanitized copy (5th patch) still map to their mount target,
        and mapping never builds or refreshes that copy (it runs on every skill_view)."""
        hermes_home, local, _shared = home
        outside = tmp_path / "outside.md"
        outside.write_text("linked", encoding="utf-8")
        (local / "productivity" / "erp-database").mkdir(parents=True)
        (local / "productivity" / "erp-database" / "SKILL.md").symlink_to(outside)
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        assert (to_agent_visible_skill_path(str(local / "productivity" / "google-workspace"))
                == "/root/.hermes/skills/productivity/google-workspace")
        assert not (hermes_home / "cache" / "skills-safe").exists()
        # ...and it is the same container root the mount list uses for that copy.
        mounts = {m["container_path"] for m in cf.get_skills_directory_mount()}
        assert "/root/.hermes/skills" in mounts

    @pytest.mark.require_symlinks
    def test_symlinked_spelling_maps_resolved_path(self, tmp_path, monkeypatch):
        real_home = tmp_path / "real-hermes"
        _skill(real_home / "skills", "cat/tool-skill")
        link_home = tmp_path / "link-hermes"
        link_home.symlink_to(real_home, target_is_directory=True)
        monkeypatch.setenv("HERMES_HOME", str(link_home))
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        assert (map_skill_path_to_container(str(real_home / "skills" / "cat" / "tool-skill"))
                == "/root/.hermes/skills/cat/tool-skill")


class TestSkillViewTool:
    def _call_tool(self, name: str) -> dict:
        from tools.skills_tool import _skill_view_with_bump
        with patch("tools.skill_usage.bump_view"), patch("tools.skill_usage.bump_use"):
            return json.loads(_skill_view_with_bump({"name": name}, task_id=None))

    def test_tool_result_reports_sandbox_skill_dir_and_content(self, home, monkeypatch):
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        result = self._call_tool("google-workspace")
        assert result["success"] is True
        assert result["skill_dir"] == "/root/.hermes/skills/productivity/google-workspace"
        assert "python /root/.hermes/skills/productivity/google-workspace/scripts/tool.py" in result["content"]
        assert str(home[0]) not in json.dumps(result)

    def test_internal_skill_view_keeps_host_path(self, home, monkeypatch):
        """Slash/preload/cron builders walk and preprocess with skill_view()'s skill_dir."""
        _home, local, _shared = home
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        from tools.skills_tool import skill_view
        result = json.loads(skill_view("google-workspace", preprocess=False))
        assert result["skill_dir"] == str(local / "productivity" / "google-workspace")

    def test_local_backend_unchanged(self, home, monkeypatch):
        _home, local, _shared = home
        monkeypatch.setenv("TERMINAL_ENV", "local")
        result = self._call_tool("google-workspace")
        assert result["skill_dir"] == str(local / "productivity" / "google-workspace")


class TestPreprocessing:
    def test_template_var_uses_sandbox_path(self, home, monkeypatch):
        _home, local, _shared = home
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        out = preprocess_skill_content("run ${HERMES_SKILL_DIR}/scripts/tool.py",
                                       local / "productivity" / "google-workspace", skills_cfg={})
        assert out == "run /root/.hermes/skills/productivity/google-workspace/scripts/tool.py"

    def test_inline_shell_snippets_keep_host_path(self, home, monkeypatch):
        """Inline shell runs on the host (cwd = host skill dir), so its snippets keep the host path."""
        _home, local, _shared = home
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        skill_dir = local / "productivity" / "google-workspace"
        out = preprocess_skill_content(
            "dir=${HERMES_SKILL_DIR} probe=!`test -f ${HERMES_SKILL_DIR}/scripts/tool.py && echo found`",
            skill_dir, skills_cfg={"inline_shell": True})
        assert out == "dir=/root/.hermes/skills/productivity/google-workspace probe=found"


class TestSkillMessageFooter:
    def test_slash_message_footer_uses_sandbox_dir(self, home, monkeypatch):
        _home, local, _shared = home
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        from agent.skill_commands import build_skill_invocation_message, scan_skill_commands
        scan_skill_commands()
        msg = build_skill_invocation_message("/google-workspace")
        assert msg is not None
        assert "[Skill directory: /root/.hermes/skills/productivity/google-workspace]" in msg
        assert "python /root/.hermes/skills/productivity/google-workspace/scripts/tool.py" in msg
        assert "`node /root/.hermes/skills/productivity/google-workspace/scripts/foo.js`" in msg
        assert "- scripts/tool.py" in msg  # supporting files still found on the host
        assert str(local) not in msg
