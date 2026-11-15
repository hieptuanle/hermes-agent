"""4handy local patch: google-workspace setup.py in a docker sandbox (no Hermes ``pm``).

When ``pm`` is missing but the Google libraries import (cloudzone: shared target dir on the sandbox user
site), ``--check`` / OAuth must continue and ``--install-deps`` must succeed without installing anything.
When they do not import, upstream's refusal (no ambient pip install) stays.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


SETUP_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/setup.py"
)

_STUBS = {
    "googleapiclient/__init__.py": "",
    "googleapiclient/discovery.py": "def build(*a, **k):\n    raise RuntimeError('stub')\n",
    "google/__init__.py": "",
    "google/oauth2/__init__.py": "",
    "google/oauth2/credentials.py": (
        "class Credentials:\n"
        "    valid = True\n"
        "    @classmethod\n"
        "    def from_authorized_user_file(cls, path):\n"
        "        return cls()\n"
    ),
    "google/auth/__init__.py": "",
    "google/auth/transport/__init__.py": "",
    "google/auth/transport/requests.py": "class Request:\n    pass\n",
    "google_auth_oauthlib/__init__.py": "",
    "google_auth_oauthlib/flow.py": "class Flow:\n    pass\n",
}


def _load_setup(monkeypatch):
    monkeypatch.syspath_prepend(str(SETUP_PATH.parent))
    spec = importlib.util.spec_from_file_location("google_workspace_setup_sandbox", SETUP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_stubs(root: Path) -> Path:
    for rel, body in _STUBS.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return root


def test_ensure_deps_continues_without_pm_when_google_imports(monkeypatch):
    module = _load_setup(monkeypatch)
    monkeypatch.setattr(module, "pm", None)
    monkeypatch.setattr(module, "_google_imports_available", lambda: True)
    monkeypatch.setattr("subprocess.check_call", Mock(side_effect=AssertionError("ambient install")))
    module._ensure_deps()  # no SystemExit
    assert module.install_deps() is True


def test_ensure_deps_still_refuses_without_pm_when_google_missing(monkeypatch, capsys):
    module = _load_setup(monkeypatch)
    monkeypatch.setattr(module, "pm", None)
    monkeypatch.setattr(module, "_google_imports_available", lambda: False)
    monkeypatch.setattr("subprocess.check_call", Mock(side_effect=AssertionError("ambient install")))
    with pytest.raises(SystemExit) as failure:
        module._ensure_deps()
    assert failure.value.code == 1
    assert "Hermes environment" in capsys.readouterr().out
    assert module.install_deps() is False


def test_google_imports_available_checks_every_module(monkeypatch, tmp_path):
    module = _load_setup(monkeypatch)
    stubs = _write_stubs(tmp_path / "stubs")
    (stubs / "google_auth_oauthlib" / "flow.py").unlink()
    monkeypatch.syspath_prepend(str(stubs))
    for name in [m for m in sys.modules if m.split(".")[0] in ("google", "googleapiclient", "google_auth_oauthlib")]:
        monkeypatch.delitem(sys.modules, name)
    assert module._google_imports_available() is False


def _run_standalone(tmp_path: Path, *args: str, token: bool = False) -> subprocess.CompletedProcess:
    """Run setup.py like a sandbox does: an interpreter without Hermes (no ``pm``) whose path holds the
    Google libraries (stubs here, put on the path via PYTHONPATH since ``-S`` drops site handling)."""
    stubs = _write_stubs(tmp_path / "stubs")
    home = tmp_path / "home"
    home.mkdir()
    if token:
        (home / "google_token.json").write_text('{"token": "t", "refresh_token": "r", "scopes": []}')
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(stubs), PATH="")
    return subprocess.run([sys.executable, "-S", str(SETUP_PATH), *args], cwd=workdir, env=env,
                          capture_output=True, text=True, timeout=30, check=False)


def test_standalone_install_deps_succeeds_when_google_importable(tmp_path):
    result = _run_standalone(tmp_path, "--install-deps")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "already importable" in result.stdout
    assert "Hermes environment" not in result.stdout
    assert "Traceback" not in result.stderr


def test_standalone_check_validates_token_when_google_importable(tmp_path):
    """With a token present ``--check`` goes through ``_ensure_deps`` (upstream exited there without pm)."""
    result = _run_standalone(tmp_path, "--check", token=True)
    assert "Hermes environment" not in result.stdout, result.stdout
    assert "AUTHENTICATED: Token valid" in result.stdout, result.stdout + result.stderr
    assert result.returncode == 0
