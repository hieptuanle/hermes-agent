"""4handy GUARD-BUDGET (2026-11-22): a data file that READS fine but would exhaust the walk budget no
longer fails the lifecycle guard closed.

#14 lets a bare-path ("exec") reference through when it cannot be READ (over the 1 MiB cap) and the
backend says it is not executable. A file under the read cap whose text still cannot be scanned (one
JSON line over the 64 KiB line cap, or more text than the walk has left) fell through to the recursion
and failed closed with "the scan budget was exhausted (text at depth 1)":

* baolinhbot cron 0409dd82a379 22/11 14:01: ``node - <<'NODE'`` (node is not an inert heredoc consumer,
  so the body is scanned as shell) + ``readFileSync('/workspace/…/pls.json')``, a 408 KB one-line JSON;
* hangngabot cron b4a3ab47d6d0 22/11 08:01: an unterminated ``python - <<'PY'`` + ``open('…/settings.json')``,
  a 725 KB one-line JSON.

Now the same authority as #14 decides: not executable → data; executable, unknown or no probe → closed.
Shapes mirror those runs with synthetic paths and content.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cron import lifecycle_guard as lg

ONE_LINE_JSON = '[{"k":"' + "v" * 400_000 + '"}]'  # under the 1 MiB read cap, over the 64 KiB line cap
assert lg._MAX_LIFECYCLE_SCAN_LINE_BYTES < len(ONE_LINE_JSON) < lg._MAX_REFERENCED_SCRIPT_BYTES


class Backend:
    """A sandbox backend: files only it can see, with their execute bits."""

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


def scan(command: str, backend: Backend | None = None, *, probe: bool = True):
    kwargs = {}
    if backend is not None:
        kwargs["read_remote_script"] = backend.read
        if probe:
            kwargs["is_remote_executable"] = backend.is_executable
    return lg.scan_gateway_lifecycle(command, cwd="/w", **kwargs)


NODE_HEREDOC = (
    "node - <<'NODE'\n"
    "const fs=require('fs'),p=JSON.parse(fs.readFileSync('/w/pls.json'));\n"
    "for(const x of p){if(/raw/i.test(x.d))console.log(x)}\n"
    "NODE"
)
UNTERMINATED_PY = "python - <<'PY'\nimport json\ns=json.load(open('/w/settings.json'))\nprint(len(s))\n"


def test_node_heredoc_reading_a_one_line_json_passes():
    """baolinhbot 22/11 14:01."""
    backend = Backend({"/w/pls.json": (ONE_LINE_JSON, False)})
    assert scan(NODE_HEREDOC, backend) == (False, None)
    assert [Path(p) for p in backend.probes] == [Path("/w/pls.json").resolve()]


def test_unterminated_python_heredoc_reading_a_one_line_json_passes():
    """hangngabot 22/11 08:01."""
    backend = Backend({"/w/settings.json": (ONE_LINE_JSON, False)})
    assert scan(UNTERMINATED_PY, backend) == (False, None)


@pytest.mark.parametrize("probe_result", [True, None])
def test_executable_or_unknown_still_blocks(probe_result):
    backend = Backend({"/w/pls.json": (ONE_LINE_JSON, probe_result)})
    unsafe, refusal = scan(NODE_HEREDOC, backend)
    assert unsafe and refusal == "the scan budget was exhausted (text at depth 1)"


def test_no_probe_keeps_the_old_fail_closed_verdict():
    backend = Backend({"/w/pls.json": (ONE_LINE_JSON, False)})
    unsafe, refusal = scan(NODE_HEREDOC, backend, probe=False)
    assert unsafe and "scan budget was exhausted" in refusal
    assert backend.probes == []


@pytest.mark.parametrize("runner", ["bash", "sh", "source", "."])
def test_a_file_run_by_a_shell_still_blocks_whatever_its_mode(runner):
    backend = Backend({"/w/x.json": (ONE_LINE_JSON, False)})
    unsafe, refusal = scan(f"{runner} /w/x.json", backend)
    assert unsafe and "scan budget was exhausted" in refusal
    assert backend.probes == []


def test_a_data_file_that_fits_is_still_scanned():
    """Only text the walk cannot afford is skipped: a small file is scanned and a lifecycle line blocks."""
    backend = Backend({"/w/pls.json": ("hermes gateway restart\n", False)})
    assert scan(NODE_HEREDOC, backend)[0] is True
    assert backend.probes == []


def test_skipping_one_file_leaves_the_budget_for_the_next():
    """The skipped text is never charged, so a later real script is still scanned and still blocks."""
    backend = Backend({
        "/w/pls.json": (ONE_LINE_JSON, False),
        "/w/after.sh": ("hermes gateway restart\n", True),
    })
    assert scan(NODE_HEREDOC + "\n/w/after.sh", backend)[0] is True


def _local(tmp_path: Path, mode: int) -> Path:
    path = tmp_path / "pls.json"
    path.write_text(ONE_LINE_JSON, encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_local_non_executable_one_line_json_passes(tmp_path):
    data = _local(tmp_path, 0o644)
    command = f"node - <<'NODE'\nrequire('fs').readFileSync('{data}')\nNODE"
    assert lg.scan_gateway_lifecycle(command) == (False, None)


def test_local_executable_one_line_file_blocks(tmp_path):
    data = _local(tmp_path, 0o755)
    command = f"node - <<'NODE'\nrequire('fs').readFileSync('{data}')\nNODE"
    unsafe, refusal = lg.scan_gateway_lifecycle(command)
    assert unsafe and "scan budget was exhausted" in refusal


def test_fits_text_charges_nothing():
    budget = lg._LifecycleScanBudget()
    before = (budget.bytes_remaining, budget.lines_remaining)
    assert budget.fits_text("echo hi\n") is True
    assert budget.fits_text(ONE_LINE_JSON) is False
    assert (budget.bytes_remaining, budget.lines_remaining) == before
