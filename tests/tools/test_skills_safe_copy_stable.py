"""4handy local patch: sanitized (symlink-free) skill copies are stable per source and refreshed in place.

Upstream's ``_safe_skills_path`` kept one per-process ``mkdtemp`` slot and ``rmtree``'d it on every call:
with two symlinked skill roots the first copy vanished before docker mounted it (``/root/.hermes/skills``
silently missing in the sandbox), and reused containers kept bind mounts to deleted copies.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import tools.credential_files as cf
from tools.credential_files import get_skills_directory_mount
from tools.environments import docker as docker_env


@pytest.fixture(autouse=True)
def _clean_state():
    cf.clear_credential_files()
    cf._config_files = {}
    yield
    cf.clear_credential_files()
    cf._config_files = {}


def _home_with_two_symlinked_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    """HERMES_HOME whose local skills/ AND one external dir both contain a symlink."""
    home = tmp_path / ".hermes"
    local = home / "skills"
    (local / "productivity" / "google-workspace" / "scripts").mkdir(parents=True)
    (local / "productivity" / "google-workspace" / "SKILL.md").write_text("# gws")
    (local / "productivity" / "google-workspace" / "scripts" / "google_api.py").write_text("print('api')")
    outside = tmp_path / "outside.md"
    outside.write_text("linked skill body")
    (local / "productivity" / "erp-database").mkdir(parents=True)
    (local / "productivity" / "erp-database" / "SKILL.md").symlink_to(outside)

    shared = tmp_path / "shared-skills"
    (shared / "productivity" / "erp-data-retrieval" / "scripts").mkdir(parents=True)
    (shared / "productivity" / "erp-data-retrieval" / "scripts" / "fetch.py").write_text("print('fetch')")
    (shared / "productivity" / "erp-cli").mkdir(parents=True)
    (shared / "productivity" / "erp-cli" / "SKILL.md").symlink_to(outside)

    (home / "config.yaml").write_text(f"skills:\n  external_dirs:\n    - {shared}\n")
    return home, local, shared


@pytest.mark.require_symlinks
def test_two_symlinked_roots_are_both_mounted(tmp_path):
    home, _local, _shared = _home_with_two_symlinked_roots(tmp_path)
    with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
        mounts = {m["container_path"]: Path(m["host_path"]) for m in get_skills_directory_mount()}

    assert set(mounts) == {"/root/.hermes/skills", "/root/.hermes/external_skills/0"}
    local_copy = mounts["/root/.hermes/skills"]
    shared_copy = mounts["/root/.hermes/external_skills/0"]
    assert local_copy.is_dir() and shared_copy.is_dir()
    assert local_copy != shared_copy
    assert (local_copy / "productivity/google-workspace/scripts/google_api.py").read_text() == "print('api')"
    assert (shared_copy / "productivity/erp-data-retrieval/scripts/fetch.py").read_text() == "print('fetch')"
    # Symlinks stay out of the copies.
    assert not (local_copy / "productivity/erp-database/SKILL.md").exists()
    assert not (shared_copy / "productivity/erp-cli/SKILL.md").exists()


@pytest.mark.require_symlinks
def test_docker_mount_args_include_local_skills_with_two_symlinked_roots(tmp_path):
    """The production failure: the docker ``-v`` list had no ``/root/.hermes/skills`` entry."""
    home, _local, _shared = _home_with_two_symlinked_roots(tmp_path)
    with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
        args = docker_env._readonly_skill_mount_args()
    specs = [a for a in args if a != "-v"]
    assert any(s.endswith(":/root/.hermes/skills:ro") for s in specs), specs
    assert any(s.endswith(":/root/.hermes/external_skills/0:ro") for s in specs), specs


@pytest.fixture
def _proc_tmp(tmp_path, monkeypatch):
    """Process tempdir apart from tmp_path (pytest's tmp_path itself sits under the system tempdir)."""
    proc_tmp = tmp_path / "proc-tmp"
    proc_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(proc_tmp))
    return proc_tmp


@pytest.mark.require_symlinks
def test_copy_is_stable_and_refreshed_in_place(tmp_path, _proc_tmp):
    home, local, _shared = _home_with_two_symlinked_roots(tmp_path)
    with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
        first = Path(cf._safe_skills_path(local))
        root_inode = first.stat().st_ino
        assert first.is_relative_to(home / "cache" / "skills-safe")
        assert not str(first).startswith(os.path.realpath(tempfile.gettempdir()) + os.sep)

        # Source changes: edit, add, remove (file and whole skill dir).
        api = local / "productivity/google-workspace/scripts/google_api.py"
        api.write_text("print('api v2')")
        os.utime(api, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        (local / "productivity/google-workspace/scripts/new_tool.py").write_text("new")
        (local / "productivity/old-skill").mkdir()
        (local / "productivity/old-skill/SKILL.md").write_text("old")
        second = Path(cf._safe_skills_path(local))
        assert (second / "productivity/old-skill/SKILL.md").exists()
        (local / "productivity/old-skill/SKILL.md").unlink()
        (local / "productivity/old-skill").rmdir()
        (local / "productivity/google-workspace/scripts/new_tool.py").unlink()
        third = Path(cf._safe_skills_path(local))

    assert first == second == third
    assert third.stat().st_ino == root_inode  # same directory: a live bind mount keeps seeing it
    assert (third / "productivity/google-workspace/scripts/google_api.py").read_text() == "print('api v2')"
    assert not (third / "productivity/google-workspace/scripts/new_tool.py").exists()
    assert not (third / "productivity/old-skill").exists()
    assert not any(p.name.startswith(".") and "hermes-tmp" in p.name for p in third.rglob("*"))


@pytest.mark.require_symlinks
def test_other_root_copy_survives_a_later_call(tmp_path):
    """Upstream's single slot deleted the previous copy on every call; a reused container's
    bind mount then pointed at a deleted directory."""
    home, local, shared = _home_with_two_symlinked_roots(tmp_path)
    with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
        local_copy = Path(cf._safe_skills_path(local))
        shared_copy = Path(cf._safe_skills_path(shared))
        again = Path(cf._safe_skills_path(local))
    assert again == local_copy
    assert (local_copy / "productivity/google-workspace/SKILL.md").exists()
    assert (shared_copy / "productivity/erp-data-retrieval/scripts/fetch.py").exists()


@pytest.mark.require_symlinks
def test_stale_entries_and_excluded_dirs_are_pruned(tmp_path):
    home, local, _shared = _home_with_two_symlinked_roots(tmp_path)
    with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
        copy = Path(cf._safe_skills_path(local))
        # Junk a crashed run or an older layout could have left behind.
        (copy / ".hub" / "x").mkdir(parents=True)
        (copy / "productivity" / ".SKILL.md.hermes-tmp-1").write_text("partial")
        (copy / "stray-link").symlink_to(tmp_path / "outside.md")
        (local / "node_modules" / "pkg").mkdir(parents=True)
        (local / "node_modules" / "pkg" / "index.js").write_text("x")
        Path(cf._safe_skills_path(local))
    assert not (copy / ".hub").exists()
    assert not (copy / "node_modules").exists()
    assert not (copy / "productivity" / ".SKILL.md.hermes-tmp-1").exists()
    assert not (copy / "stray-link").is_symlink()


@pytest.mark.require_symlinks
def test_stable_copy_is_not_a_volatile_mount(tmp_path, _proc_tmp):
    home, local, _shared = _home_with_two_symlinked_roots(tmp_path)
    with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
        copy = cf._safe_skills_path(local)
    assert not docker_env._is_volatile_mount_spec(f"{copy}:/root/.hermes/skills:ro")
