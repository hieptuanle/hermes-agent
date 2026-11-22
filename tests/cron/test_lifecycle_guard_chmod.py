"""4handy GUARD-CHMOD (hreview S1, 2026-11-22): the data skip never applies once the walked command can
change a file mode.

#14 (a file over the 1 MiB read cap) and GUARD-BUDGET Fix A (a readable file whose text would exhaust the
walk budget) let a bare-path ("exec") reference through when its execute bit is off. The bit is read at
SCAN time, so ``chmod +x f && ./f`` on a 0644 ``f`` holding a lifecycle command passed. Now any walked text
that can change a mode (chmod in any spelling, setfacl, ``install`` in command position) disables the skip
for the rest of the walk: such a file fails closed again, as before #14.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from cron import lifecycle_guard as lg

RESTART = "hermes gateway restart\n"
FIXA_TEXT = RESTART + "#" + "x" * 400_000 + "\n"  # readable, one line over the 64 KiB cap (Fix A class)
P14_TEXT = RESTART + "#" + "x" * 2048 + "\n"  # over the read cap once it is lowered to 1 KiB (#14 class)


@pytest.fixture(params=["p14", "fixa"])
def big(request, monkeypatch):
    """Unscannable script text in each class the data skip covers."""
    if request.param == "p14":
        monkeypatch.setattr(lg, "_MAX_REFERENCED_SCRIPT_BYTES", 1024)
        return P14_TEXT
    return FIXA_TEXT


class Backend:
    def __init__(self, files: dict[str, tuple[str, bool | None]]):
        self.files = {str(Path(k).resolve()): v for k, v in files.items()}
        self.probes: list[str] = []

    def read(self, path: str):
        hit = self.files.get(str(Path(path).resolve()))
        return None if hit is None else hit[0]

    def is_executable(self, path: str):
        self.probes.append(path)
        hit = self.files.get(str(Path(path).resolve()))
        return None if hit is None else hit[1]


def scan(command: str, backend: Backend):
    return lg.scan_gateway_lifecycle(command, cwd="/w", read_remote_script=backend.read,
                                     is_remote_executable=backend.is_executable)


@pytest.mark.parametrize("command", [
    "chmod +x /w/f && /w/f",
    "chmod 755 /w/f; /w/f --go",
    "sudo chmod u+x /w/f && timeout 60 /w/f",
    "install -m 0755 /w/src /w/f && /w/f",
    "sudo install /w/src /w/f; /w/f",
    "cd /w && /usr/bin/install -D /w/src /w/f && /w/f",
    "setfacl -m u::rwx /w/f && /w/f",
    "python3 - <<'PY'\nimport os\nos.chmod('/w/f', 0o755)\nPY\n/w/f",
    "node -e \"require('fs').chmodSync('/w/f', 0o755)\" && /w/f",
])
def test_a_mode_change_in_the_command_disables_the_data_skip(big, command):
    backend = Backend({"/w/f": (big, False)})
    unsafe, refusal = scan(command, backend)
    assert unsafe and refusal
    assert backend.probes == []  # the stale execute bit is not even asked


def test_without_the_mode_change_the_same_file_is_still_data(big):
    backend = Backend({"/w/f": (big, False)})
    assert scan("/w/f", backend) == (False, None)


def test_chmod_in_a_script_run_first_disables_the_skip_for_later_candidates(big):
    backend = Backend({"/w/setup.sh": ("chmod +x /w/f\n", True), "/w/f": (big, False)})
    assert scan("bash /w/setup.sh && /w/f", backend)[0] is True


def test_a_mode_change_is_reported_in_the_block_log(big, caplog):
    backend = Backend({"/w/f": (big, False)})
    with caplog.at_level(logging.INFO, logger="cron.lifecycle_guard"):
        scan("chmod +x /w/f && /w/f", backend)
    if big is P14_TEXT:  # the unreadable path names why the skip did not apply
        assert "change file modes" in caplog.text
    assert "treated as data" not in caplog.text


# --- installers and the 22/11 shapes keep the data skip ----------------------------------------

NODE_HEREDOC = "node - <<'NODE'\nconst p=JSON.parse(require('fs').readFileSync('/w/pls.json'))\nNODE"
ONE_LINE_JSON = '[{"k":"' + "v" * 400_000 + '"}]'


@pytest.mark.parametrize("prefix", [
    "",
    "pip install -q requests && ",
    "npm install && ",
    "apt-get install -y jq; ",
    "uv pip install x && ",
    "echo installed && ",
])
def test_package_installs_and_plain_commands_keep_the_data_skip(prefix):
    backend = Backend({"/w/pls.json": (ONE_LINE_JSON, False)})
    assert scan(prefix + NODE_HEREDOC, backend) == (False, None)


def test_unterminated_python_heredoc_shape_still_passes():
    backend = Backend({"/w/settings.json": (ONE_LINE_JSON, False)})
    command = "python - <<'PY'\nimport json\ns=json.load(open('/w/settings.json'))\nprint(len(s))\n"
    assert scan(command, backend) == (False, None)


def test_an_unrelated_chmod_also_disables_the_skip_by_design():
    """Deliberately broad: a false hit only restores the pre-#14 fail-closed verdict."""
    backend = Backend({"/w/pls.json": (ONE_LINE_JSON, False)})
    assert scan("chmod 600 /w/key && " + NODE_HEREDOC, backend)[0] is True


def test_small_scannable_files_are_unaffected():
    backend = Backend({"/w/notes.json": ('{"a": 1}', False)})
    assert scan("chmod 600 /w/key && /w/notes.json", backend) == (False, None)


def test_local_mode_change_then_exec_blocks(tmp_path):
    f = tmp_path / "f"
    f.write_text(FIXA_TEXT, encoding="utf-8")
    os.chmod(f, 0o644)
    assert lg.scan_gateway_lifecycle(f"{f}") == (False, None)
    unsafe, refusal = lg.scan_gateway_lifecycle(f"chmod +x {f} && {f}")
    assert unsafe and "scan budget was exhausted" in refusal
