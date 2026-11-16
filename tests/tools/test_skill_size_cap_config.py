"""4handy local patch: configurable, growth-only SKILL.md size cap (``skills.max_content_chars``).

The post-turn background review kept patching business skills until SKILL.md hit the fixed 100k cap.
The cap is now configurable, and for an existing SKILL.md it only refuses writes that make an over-cap
file bigger, so the review and the curator can always trim. Supporting files keep the fixed limits.
"""

import json

import pytest

from tools.skill_manager_tool import MAX_SKILL_CONTENT_CHARS, _skill_md_char_cap, skill_manage


@pytest.fixture
def home(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr("tools.skill_manager_tool.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills_dir)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _set_cap(home, value) -> None:
    (home / "config.yaml").write_text(f"skills:\n  max_content_chars: {value}\n", encoding="utf-8")


def _content(name: str, total_chars: int, filler: str = "x") -> str:
    head = f"---\nname: {name}\ndescription: Test skill {name}.\n---\n\n# {name}\n\nMARK\n"
    return head + filler * max(0, total_chars - len(head))


def _place(home, name: str, total_chars: int) -> str:
    """Put a SKILL.md on disk directly (as earlier writes under a bigger cap left it)."""
    text = _content(name, total_chars)
    skill_dir = home / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return text


def _md(home, name: str) -> str:
    return (home / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def _call(**kwargs) -> dict:
    return json.loads(skill_manage(**kwargs))


class TestCapConfig:
    def test_unset_uses_upstream_default(self, home):
        assert _skill_md_char_cap() == MAX_SKILL_CONTENT_CHARS == 100_000
        assert _call(action="create", name="at-cap", content=_content("at-cap", 100_000))["success"] is True
        assert _call(action="create", name="over", content=_content("over", 100_001))["success"] is False

    def test_configured_value_is_used(self, home):
        _set_cap(home, 40000)
        assert _skill_md_char_cap() == 40_000

    @pytest.mark.parametrize("value", ["0", "-5", "abc", "true", "null", "[1, 2]"])
    def test_invalid_value_falls_back_to_default(self, home, value):
        _set_cap(home, value)
        assert _skill_md_char_cap() == MAX_SKILL_CONTENT_CHARS


class TestGrowthOnly:
    @pytest.fixture(autouse=True)
    def _cap_40k(self, home):
        _set_cap(home, 40000)

    def test_create_over_cap_refused_with_fix_hint(self, home):
        result = _call(action="create", name="big-new", content=_content("big-new", 41_000))
        assert result["success"] is False
        err = result["error"]
        assert "40,000" in err and "skills.max_content_chars" in err
        assert "references/<topic>.md" in err and "write_file" in err
        assert "smaller is always allowed" in err
        assert not (home / "skills" / "big-new").exists()

    def test_create_under_cap_allowed(self, home):
        assert _call(action="create", name="small-new", content=_content("small-new", 39_000))["success"] is True

    def test_patch_growing_over_cap_skill_refused(self, home):
        before = _place(home, "biz", 60_000)
        result = _call(action="patch", name="biz", old_string="MARK", new_string="MARK plus one more line")
        assert result["success"] is False
        assert "60,000" in result["error"] and "references/" in result["error"]
        assert _md(home, "biz") == before

    def test_patch_shrinking_over_cap_skill_allowed(self, home):
        _place(home, "biz", 99_000)
        result = _call(action="patch", name="biz", old_string="MARK\n" + "x" * 500, new_string="MARK\n")
        assert result["success"] is True, result
        assert len(_md(home, "biz")) == 99_000 - 500

    def test_patch_same_size_over_cap_allowed(self, home):
        _place(home, "biz", 50_000)
        assert _call(action="patch", name="biz", old_string="MARK", new_string="MAKR")["success"] is True

    def test_patch_growing_under_cap_allowed(self, home):
        _place(home, "biz", 30_000)
        result = _call(action="patch", name="biz", old_string="MARK", new_string="MARK\n" + "y" * 9_000)
        assert result["success"] is True, result
        assert len(_md(home, "biz")) == 30_000 + 9_000 + 1

    def test_patch_growing_across_cap_refused(self, home):
        _place(home, "biz", 39_900)
        result = _call(action="patch", name="biz", old_string="MARK", new_string="MARK\n" + "y" * 200)
        assert result["success"] is False

    def test_edit_shrinking_over_cap_allowed_growing_refused(self, home):
        _place(home, "biz", 70_000)
        assert _call(action="edit", name="biz", content=_content("biz", 70_500))["success"] is False
        assert _call(action="edit", name="biz", content=_content("biz", 45_000))["success"] is True
        assert len(_md(home, "biz")) == 45_000

    def test_write_file_on_skill_md_is_growth_only(self, home):
        _place(home, "biz", 50_000)
        grow = _call(action="write_file", name="biz", file_path="SKILL.md", file_content=_content("biz", 50_100))
        assert grow["success"] is False
        shrink = _call(action="write_file", name="biz", file_path="SKILL.md", file_content=_content("biz", 41_000))
        assert shrink["success"] is True, shrink

    def test_patch_skill_md_via_file_path_is_growth_only(self, home):
        _place(home, "biz", 50_000)
        result = _call(action="patch", name="biz", file_path="SKILL.md", old_string="MARK", new_string="MARK!")
        assert result["success"] is False

    def test_supporting_files_keep_fixed_limit(self, home):
        """The fix the error message points to must work: references/ is not bound by the SKILL.md cap."""
        _place(home, "biz", 60_000)
        result = _call(action="write_file", name="biz", file_path="references/depth.md",
                       file_content="# Depth\n\n" + "z" * 60_000)
        assert result["success"] is True, result
        trim = _call(action="patch", name="biz", old_string="MARK\n" + "x" * 30_000,
                     new_string="MARK\nSee references/depth.md.\n")
        assert trim["success"] is True, trim
        assert len(_md(home, "biz")) < 40_000
