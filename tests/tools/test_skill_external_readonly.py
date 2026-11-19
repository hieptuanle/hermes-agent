"""4handy local patch: ``skills.external_writable: false`` makes shared skills read-only for bots.

Skills in ``skills.external_dirs`` are shared by every bot on the host. Bots kept patching them (one chat
patched a shared skill 47 times), and in a git-synced dir the edits were silently reset. With the key false,
every skill_manage write to a shared skill is refused with a non-transient message that points the bot at a
local skill; reads are unchanged; local and project skills keep upstream behaviour.
"""

import json
from pathlib import Path

import pytest

from agent.skill_utils import _external_dirs_cache_clear
from tools.skill_manager_tool import _is_shared_skill_path, skill_manage

SKILL = "---\nname: {name}\ndescription: Skill {name} for tests.\n---\n\n# {name}\n\nDo the thing.\n"


def _write_skill(root: Path, rel: str) -> Path:
    d = root / rel
    (d / "references").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(SKILL.format(name=d.name), encoding="utf-8")
    (d / "references" / "old.md").write_text("# Old\n", encoding="utf-8")
    return d


def _tree(path: Path) -> dict:
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    local = home / "skills"
    shared = tmp_path / "shared-skills"
    _write_skill(local, "productivity/my-local")
    _write_skill(shared, "productivity/erp-cli")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("tools.skill_manager_tool.SKILLS_DIR", local)
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", local)

    def configure(writable=None, extra=""):
        line = "" if writable is None else f"  external_writable: {writable}\n"
        (home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {shared}\n{line}{extra}", encoding="utf-8")
        _external_dirs_cache_clear()

    configure()
    yield home, local, shared, configure
    _external_dirs_cache_clear()


def _call(**kw) -> dict:
    return json.loads(skill_manage(**kw))


SHARED_WRITES = [
    dict(action="patch", name="erp-cli", old_string="Do the thing.", new_string="Do it differently."),
    dict(action="patch", name="erp-cli", content=SKILL.format(name="erp-cli") + "\nMore.\n"),
    dict(action="edit", name="erp-cli", content=SKILL.format(name="erp-cli") + "\nMore.\n"),
    dict(action="write_file", name="erp-cli", file_path="references/new.md", file_content="# New\n"),
    dict(action="write_file", name="erp-cli", file_path="references/old.md", file_content="# Changed\n"),
    dict(action="patch", name="erp-cli", file_path="references/old.md", old_string="Old", new_string="New"),
    dict(action="remove_file", name="erp-cli", file_path="references/old.md"),
    dict(action="delete", name="erp-cli"),
    dict(action="delete", name="erp-cli", absorbed_into="my-local"),
]


def _assert_refusal(result: dict) -> None:
    assert result["success"] is False, result
    err = result["error"]
    assert "not a temporary error" in err and "read-only for bots" in err
    assert "LOCAL skill" in err and "Do not retry" in err and "skills.external_dirs" in err


class TestReadOnly:
    @pytest.mark.parametrize("kw", SHARED_WRITES, ids=lambda kw: f"{kw['action']}-{kw.get('file_path') or ''}")
    def test_every_write_refused_and_files_unchanged(self, env, kw):
        _home, _local, shared, configure = env
        configure(False)
        before = _tree(shared)
        _assert_refusal(_call(**kw))
        assert _tree(shared) == before

    @pytest.mark.parametrize("value", ["false", "'false'", "'no'", "'off'", "0"])
    def test_false_spellings(self, env, value):
        _home, _local, shared, configure = env
        configure(value)
        before = _tree(shared)
        _assert_refusal(_call(**SHARED_WRITES[0]))
        assert _tree(shared) == before

    def test_create_into_shared_create_dir_refused(self, env):
        _home, _local, shared, configure = env
        configure(False, extra=f"  create_dir: {shared}\n")
        result = _call(action="create", name="brand-new", content=SKILL.format(name="brand-new"))
        _assert_refusal(result)
        assert not (shared / "brand-new").exists()

    def test_create_with_shared_name_hints_rename(self, env):
        _home, _local, _shared, configure = env
        configure(False)
        result = _call(action="create", name="erp-cli", content=SKILL.format(name="erp-cli"))
        assert result["success"] is False
        assert "already exists" in result["error"] and "pick a different name" in result["error"]

    def test_background_review_refused(self, env):
        from tools.skill_manager_guards import mark_background_review_skill_read
        from tools.skill_provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin
        _home, _local, shared, configure = env
        configure(False)
        before = _tree(shared)
        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            mark_background_review_skill_read(shared / "productivity" / "erp-cli" / "SKILL.md")
            for kw in SHARED_WRITES[:7]:
                _assert_refusal(_call(**kw))
        finally:
            reset_current_write_origin(token)
        assert _tree(shared) == before

    def test_batch_with_a_shared_op_is_refused_and_rolled_back(self, env):
        _home, local, shared, configure = env
        configure(False)
        before_shared, before_local = _tree(shared), _tree(local)
        result = _call(action="patch", name="my-local", operations=[
            {"action": "write_file", "name": "my-local", "file_path": "references/lesson.md",
             "file_content": "# Lesson\n"},
            SHARED_WRITES[0]])
        assert result["success"] is False and "read-only for bots" in result["error"]
        assert _tree(shared) == before_shared
        assert {k: v for k, v in _tree(local).items() if not k.startswith(".")} == \
            {k: v for k, v in before_local.items() if not k.startswith(".")}

    def test_local_skills_stay_writable(self, env):
        _home, local, _shared, configure = env
        configure(False)
        assert _call(action="patch", name="my-local", old_string="Do the thing.",
                     new_string="Do the new thing.")["success"] is True
        assert _call(action="write_file", name="my-local", file_path="references/lesson.md",
                     file_content="# Lesson\n")["success"] is True
        assert _call(action="create", name="fresh-local", content=SKILL.format(name="fresh-local"))["success"] is True
        assert (local / "fresh-local" / "SKILL.md").exists()
        assert _call(action="delete", name="fresh-local")["success"] is True

    def test_reads_unchanged(self, env):
        from tools.skills_tool import skill_view
        _home, _local, _shared, configure = env
        configure(False)
        view = json.loads(skill_view("erp-cli", preprocess=False))
        assert view["success"] is True and "Do the thing." in view["content"]

    def test_shared_path_helper(self, env):
        _home, local, shared, configure = env
        configure(False)
        assert _is_shared_skill_path(shared / "productivity" / "erp-cli")
        assert not _is_shared_skill_path(local / "productivity" / "my-local")


class TestUpstreamBehaviour:
    @pytest.mark.parametrize("value", [None, "true", "'yes'"])
    def test_shared_skill_writable(self, env, value):
        _home, _local, shared, configure = env
        configure(value)
        result = _call(**SHARED_WRITES[0])
        assert result["success"] is True, result
        assert "Do it differently." in (shared / "productivity" / "erp-cli" / "SKILL.md").read_text(encoding="utf-8")
