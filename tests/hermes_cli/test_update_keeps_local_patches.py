"""4handy local patch: `hermes update` must not run code without the checkout's local patches.

Upstream (v0.21.7+) lands a stable release DETACHED at its tag and switches a parked branch back to
``main``; both keep local commits in a ref but the running tree loses them. The guard refuses those
moves before anything is stashed or written. Real git repositories, no mocked subprocess.
"""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli import update_cmd

GIT = ["git"]


def _git(repo, *args, check=True):
    return subprocess.run(GIT + list(args), cwd=repo, capture_output=True, text=True, check=check)


def _commit(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def patched_checkout(tmp_path, monkeypatch):
    """Clone of an upstream with release tags v1.0.0 and v1.1.0, on branch ``4handy/v1.0.0`` = v1.0.0 + 1 patch."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _commit(upstream, "core.txt", "core\n")
    _git(upstream, "tag", "v1.0.0")
    _commit(upstream, "next.txt", "next\n")
    _git(upstream, "tag", "v1.1.0")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(upstream), str(checkout))
    _git(checkout, "checkout", "-q", "-b", "4handy/v1.0.0", "v1.0.0")
    patch = _commit(checkout, "LOCAL_PATCHES.md", "# local patches\n")
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", checkout)
    return checkout, patch


def _prepare(current_branch, *, target_ref=None, switch_branch=False):
    return update_cmd._prepare_checkout_for_update(
        GIT, "main", current_branch, is_fork=False, assume_yes=True, gateway_mode=False, gw_input_fn=None,
        switch_branch=switch_branch, target_ref=target_ref, _windows_gateway_resume=None)


def test_local_only_commit_count_ignores_origin_and_release_tags(patched_checkout):
    checkout, _patch = patched_checkout
    assert update_cmd._local_only_commit_count(GIT) == 1
    _git(checkout, "checkout", "-q", "--detach", "v1.1.0")
    assert update_cmd._local_only_commit_count(GIT) == 0


def test_release_update_refuses_to_detach_off_local_patches(patched_checkout, capsys):
    checkout, patch = patched_checkout
    with pytest.raises(SystemExit) as exc:
        _prepare("4handy/v1.0.0", target_ref="v1.1.0")
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "1 local commit(s)" in out and "No update was applied" in out
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == patch
    assert _git(checkout, "branch", "--show-current").stdout.strip() == "4handy/v1.0.0"


def test_release_update_refuses_even_with_switch_branch(patched_checkout):
    with pytest.raises(SystemExit):
        _prepare("4handy/v1.0.0", target_ref="v1.1.0", switch_branch=True)


def test_branch_update_refuses_to_switch_parked_branch_back_to_main(patched_checkout, capsys):
    checkout, patch = patched_checkout
    with pytest.raises(SystemExit):
        _prepare("4handy/v1.0.0")
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == patch
    assert "local commit(s)" in capsys.readouterr().out


def test_update_in_place_strategy_is_allowed(patched_checkout, monkeypatch):
    """parked_branch_strategy=update_in_place merges origin/main INTO the branch: patches stay live."""
    monkeypatch.setattr(update_cmd, "_updates_config", lambda: {"parked_branch_strategy": "update_in_place"})
    plan = _prepare("4handy/v1.0.0")
    assert plan.in_place_update is True


def test_explicit_switch_branch_overrides_on_branch_path(patched_checkout):
    checkout, _patch = patched_checkout
    _prepare("4handy/v1.0.0", switch_branch=True)
    assert _git(checkout, "branch", "--show-current").stdout.strip() == "main"


def test_pure_upstream_checkout_is_not_blocked(patched_checkout):
    checkout, _patch = patched_checkout
    _git(checkout, "checkout", "-q", "main")
    plan = _prepare("main", target_ref="v1.1.0")
    assert plan.in_place_update is True


def test_guard_is_inert_without_local_patches_md(patched_checkout):
    """Upstream behaviour is untouched in a checkout that does not declare local patches."""
    checkout, _patch = patched_checkout
    _git(checkout, "rm", "-q", "LOCAL_PATCHES.md")
    _commit(checkout, "other.txt", "x\n")
    plan = _prepare("4handy/v1.0.0", target_ref="v1.1.0")
    assert plan.in_place_update is True
