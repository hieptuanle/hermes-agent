"""4handy local patch #14: data files named in a command no longer fail the lifecycle guard closed.

A bare path in shell command position counts as an executed script. Python/JS code tokenized as shell
puts paths there (``Path('/w/x.json')`` splits at ``(``), so a >1 MiB JSON data file failed the guard
closed and blocked the whole command (14 blocks fleet-wide 15-20/11). Now:

* a bare-path ("exec") candidate that cannot be scanned blocks only when it is executable or its mode
  is unknown (the kernel never execs a file without an execute bit, whatever its extension);
* a file run through a shell (``bash x``, ``sh x``, ``source x``, ``. x``) still always blocks;
* inside a kernel-executed non-shell program (node/python shebang), paths are mentions.

Shapes below mirror the 20/11 blocks with synthetic paths and content.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from cron import lifecycle_guard as lg
from tools import terminal_tool_guards as ttg

CAP = 1024
BIG = "[" + ",".join(['{"k": "v"}'] * 400) + "]"  # > CAP, JSON
assert len(BIG) > CAP


@pytest.fixture(autouse=True)
def _small_cap(monkeypatch):
    monkeypatch.setattr(lg, "_MAX_REFERENCED_SCRIPT_BYTES", CAP)


class Backend:
    """A sandbox backend: files only it can see, with their execute bits."""

    def __init__(self, files: dict[str, tuple[str, bool]]):
        self.files = {str(Path(k).resolve()): v for k, v in files.items()}
        self.reads: list[str] = []
        self.probes: list[str] = []

    def read(self, path: str):
        self.reads.append(path)
        hit = self.files.get(str(Path(path).resolve()))
        return None if hit is None else hit[0]

    def is_executable(self, path: str):
        self.probes.append(path)
        hit = self.files.get(str(Path(path).resolve()))
        return None if hit is None else hit[1]


def scan(command: str, backend: Backend | None = None, *, cwd: str | None = None, probe: bool = True):
    kwargs = {}
    if backend is not None:
        kwargs["read_remote_script"] = backend.read
        if probe:
            kwargs["is_remote_executable"] = backend.is_executable
    return lg.scan_gateway_lifecycle(command, cwd=cwd, **kwargs)


def _local(tmp_path: Path, name: str, text: str, mode: int = 0o644) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


# --- the 20/11 shapes: data arguments pass ------------------------------------------------------

def test_python_heredoc_reading_big_json_on_backend_passes():
    """kyduyenbot 21:38/22:30: `cd /w && D=$(date) && python - <<'PY'` + Path('/w/x.json')."""
    backend = Backend({"/w/cands.json": (BIG, False)})
    command = (
        "cd /w && D=$(TZ=Asia/Ho_Chi_Minh date +%F) && python - <<'PY'\n"
        "import json\nfrom pathlib import Path\n"
        "p=Path('/w/cands.json'); x=json.loads(p.read_text())\n"
        "Path(f\"/w/out_{x['today']}.json\").write_text('{}')\n"
        "PY\n"
        "CHECKPOINT=/w/out_${D}.json node run.mjs > /w/run.log 2>&1"
    )
    assert scan(command, backend, cwd="/w") == (False, None)
    assert [Path(p) for p in backend.probes] == [Path("/w/cands.json").resolve()]


def test_node_heredoc_requiring_big_json_on_backend_passes():
    """huyhoangbot 08:03: `node - <<'NODE'` + require('/w/x.json')."""
    backend = Backend({"/w/requests.json": (BIG, False)})
    command = "node - <<'NODE'\nconst a=require('/w/requests.json'); console.log(a.length)\nNODE"
    assert scan(command, backend, cwd="/w") == (False, None)


def test_python_c_inside_command_substitution_passes():
    """baolinhbot 13:32: nested quotes leak `open('/w/x.json')` into shell tokens."""
    backend = Backend({"/w/live.json": (BIG, False), "/w/cats.json": (BIG, False)})
    command = (
        'erp api GET /products/lookup -q ids="$(python -c "import json;'
        "l=json.load(open('/w/live.json'));p=json.load(open('/w/cats.json'));print(len(l))\")\" "
        "> /w/orphans.json; python -c \"import json;print(json.load(open('/w/orphans.json')))\""
    )
    assert scan(command, backend, cwd="/w") == (False, None)


def test_node_program_with_many_path_strings_does_not_exhaust_remote_budget(monkeypatch):
    """long_d2_bot 15:17/15:18: `/opt/t/bin/zca ...` is a node bundle full of '/x' strings."""
    monkeypatch.setattr(lg, "_MAX_LIFECYCLE_SCAN_REMOTE_READS", 8)
    bundle = "#!/usr/bin/env node\n" + "\n".join(f"f('/opt/t/lib/p{i}.js');" for i in range(40))
    backend = Backend({"/opt/t/bin/zca": (bundle, True)})
    assert scan("/opt/t/bin/zca account --help; /opt/t/bin/zca account list", backend) == (False, None)


def test_local_big_non_executable_json_passes(tmp_path):
    data = _local(tmp_path, "cands.json", BIG)
    command = f"python3 - <<'PY'\nimport json\nx=json.load(open('{data}'))\nPY\necho $?"
    assert scan(f"D=$(date) && {command}") == (False, None)


def test_real_size_file_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(lg, "_MAX_REFERENCED_SCRIPT_BYTES", 1024 * 1024)
    data = tmp_path / "cands.json"
    data.write_bytes(b"[" + b'{"invoice": "x"},' * 140_000 + b"{}]")  # ~2.4 MB like 20/11
    assert data.stat().st_size > 2 * 1024 * 1024
    command = f"cd {tmp_path} && D=$(date +%F) && python - <<'PY'\nfrom pathlib import Path\nPath('{data}').read_text()\nPY"
    assert scan(command) == (False, None)


# --- the program stays closed --------------------------------------------------------------------

@pytest.mark.parametrize("runner", ["bash", "sh", "source", ".", "sudo bash", "timeout 60 sh"])
def test_unscannable_file_run_by_a_shell_blocks_whatever_its_extension_or_mode(runner):
    backend = Backend({"/w/x.json": (BIG, False)})
    unsafe, refusal = scan(f"{runner} /w/x.json", backend, cwd="/w")
    assert unsafe and "scan cap" in refusal


@pytest.mark.parametrize("runner", ["bash", "sh", "source", ".", "env FOO=1 bash"])
def test_local_unscannable_non_executable_file_run_by_a_shell_blocks(tmp_path, runner):
    data = _local(tmp_path, "x.json", BIG)  # 0644: as a bare path it would be data
    unsafe, refusal = scan(f"{runner} {data}")
    assert unsafe and "scan cap" in refusal


@pytest.mark.parametrize("probe_result", [True, None])
def test_unscannable_bare_path_blocks_when_executable_or_unknown(probe_result):
    """`exec of the path`: x.json with an execute bit (or unknown mode) is a program."""
    backend = Backend({"/w/x.json": (BIG, probe_result)})
    unsafe, refusal = scan("/w/x.json --run", backend, cwd="/w")
    assert unsafe and "scan cap" in refusal


def test_unscannable_bare_path_blocks_without_a_probe():
    """An older caller passes no probe: behaviour is exactly the pre-#14 fail-closed one."""
    backend = Backend({"/w/x.json": (BIG, False)})
    unsafe, _ = scan("/w/x.json", backend, cwd="/w", probe=False)
    assert unsafe


def test_local_executable_big_file_blocks_even_as_json(tmp_path):
    tool = _local(tmp_path, "tool.json", "#!/bin/sh\n" + BIG, mode=0o755)
    unsafe, refusal = scan(f"{tool} --go")
    assert unsafe and "scan cap" in refusal


def test_local_executable_big_file_in_python_heredoc_still_blocks(tmp_path):
    """subprocess.run('/x/tool') puts the path in command position too; an executable stays closed."""
    tool = _local(tmp_path, "tool", "#!/bin/sh\n" + BIG, mode=0o755)
    command = f"python3 - <<'PY'\nimport subprocess\nsubprocess.run('{tool}')\nPY"
    assert scan(f"D=$(date) && {command}")[0]


def test_readable_data_file_is_still_scanned(tmp_path):
    """Only UNSCANNABLE data is let through: a small file is read and a lifecycle line in it blocks."""
    small = _local(tmp_path, "notes.json", "hermes gateway restart\n")
    command = f"D=$(date) && python - <<'PY'\nopen('{small}').read()\nPY"
    assert scan(command)[0] is True


def test_executed_node_program_with_literal_restart_blocks():
    bundle = "#!/usr/bin/env node\nrequire('child_process').execSync('hermes gateway restart')\n"
    backend = Backend({"/opt/t/bin/tool": (bundle, True)})
    assert scan("/opt/t/bin/tool", backend)[0] is True


def test_executed_big_node_program_blocks():
    backend = Backend({"/opt/t/bin/tool": ("#!/usr/bin/env node\n" + BIG, True)})
    unsafe, refusal = scan("/opt/t/bin/tool", backend)
    assert unsafe and "scan cap" in refusal


def test_bash_ignores_a_node_shebang(monkeypatch):
    """`bash x` runs x as shell whatever its shebang: its paths stay executed (budget still closes)."""
    monkeypatch.setattr(lg, "_MAX_LIFECYCLE_SCAN_REMOTE_READS", 4)
    fake = "#!/usr/bin/env node\n" + "\n".join(f"/opt/t/lib/p{i}.sh" for i in range(10))
    backend = Backend({"/w/x.js": (fake, False)})
    unsafe, refusal = scan("bash /w/x.js", backend, cwd="/w")
    assert unsafe and "budget" in refusal


def test_shell_script_paths_still_exhaust_the_budget(monkeypatch):
    monkeypatch.setattr(lg, "_MAX_LIFECYCLE_SCAN_REMOTE_READS", 4)
    script = "#!/bin/sh\n" + "\n".join(f"/opt/t/lib/p{i}.sh" for i in range(10))
    backend = Backend({"/opt/t/bin/tool": (script, True)})
    unsafe, refusal = scan("/opt/t/bin/tool", backend)
    assert unsafe and "budget" in refusal


def test_shebang_parsing():
    f = lg._is_non_shell_program_text
    assert f("#!/usr/bin/env node\n") and f("#!/usr/bin/python3 -u\n") and f("#!/usr/bin/env -S deno run\n")
    assert not f("#!/bin/sh\n") and not f("#!/usr/bin/env bash\n") and not f("#!/bin/bash -e\n")
    assert not f("echo hi\n") and not f("#!\n") and not f(None)


# --- terminal hook --------------------------------------------------------------------------------

class FakeEnv:
    def __init__(self, code):
        self.code = code
        self.commands = []

    def execute(self, command):
        self.commands.append(command)
        return {"returncode": self.code, "output": ""}


@pytest.mark.parametrize("code,expected", [(0, True), (1, False), (127, None)])
def test_backend_probe(code, expected):
    env = FakeEnv(code)
    assert ttg._is_executable_for_guard(env, "/w", "/w/x y.json") is expected
    assert env.commands == ["test -x " + shlex.quote("/w/x y.json")]


def test_backend_probe_never_uses_the_host_mode(tmp_path):
    """Review P14 F1: the host may hold a different file at the same path; only the backend answers."""
    data = _local(tmp_path, "d.json", "{}")  # 0644 on the host
    env = FakeEnv(0)
    assert ttg._is_executable_for_guard(env, str(tmp_path), str(data)) is True
    assert env.commands == ["test -x " + shlex.quote(str(data))]


def test_probe_kwarg_only_for_a_guard_that_accepts_it():
    """A gateway that imported the pre-#14 guard module must not get an unexpected keyword."""
    def old_scan(command, *, cwd=None, read_remote_script=None):
        return False, None

    assert ttg._lifecycle_scan_kwargs(old_scan, FakeEnv(0), "/w", "docker") == {}
    assert set(ttg._lifecycle_scan_kwargs(lg.scan_gateway_lifecycle, FakeEnv(0), "/w", "docker")) == {
        "is_remote_executable"}


def test_unreadable_signature_still_gets_the_backend_probe(monkeypatch):
    """Review P14 nit 1: if ``inspect.signature`` fails, a remote backend must not fall back to the host
    stat; the probe is passed anyway (a guard that rejected it would raise and fail closed)."""
    import inspect

    def boom(_obj, *a, **kw):
        raise ValueError("no signature")

    monkeypatch.setattr(inspect, "signature", boom)
    assert set(ttg._lifecycle_scan_kwargs(lg.scan_gateway_lifecycle, FakeEnv(0), "/w", "docker")) == {
        "is_remote_executable"}
    assert ttg._lifecycle_scan_kwargs(lg.scan_gateway_lifecycle, FakeEnv(0), "/w", "local") == {}


@pytest.mark.parametrize("env_type,probe", [("local", False), ("docker", True), ("ssh", True), (None, True)])
def test_probe_only_for_a_backend_that_is_not_the_host(env_type, probe):
    """Local backend: the host file IS the file that runs, so the guard uses the host mode (no probe)."""
    kwargs = ttg._lifecycle_scan_kwargs(lg.scan_gateway_lifecycle, FakeEnv(0), "/w", env_type)
    assert ("is_remote_executable" in kwargs) is probe


# --- review P14 F1: the host's file must not decide for the backend's ---------------------------

def test_host_file_unreadable_and_not_executable_backend_copy_executable_blocks(tmp_path):
    """Host: same path exists, unreadable (so the guard reads the backend copy), mode 0. Backend: the
    copy is executable, unscannable and holds a restart. Parent blocks; so must #14."""
    host = _local(tmp_path, "shadow", "secret\n", mode=0o000)
    payload = "#!/bin/sh\nhermes gateway restart\n" + "#" * (CAP * 2)
    backend = Backend({str(host): (payload, True)})
    try:
        unsafe, refusal = scan(f"{host}", backend, cwd=str(tmp_path))
    finally:
        os.chmod(host, 0o644)
    assert unsafe and "scan cap" in refusal


def test_host_file_big_readable_not_executable_backend_copy_executable_blocks(tmp_path):
    """Host: same path is a big readable 0644 file (unscannable locally). With a backend probe, only
    the backend's mode counts: executable there -> block."""
    host = _local(tmp_path, "status", BIG)
    backend = Backend({str(host): (BIG, True)})
    unsafe, refusal = scan(f"{host}", backend, cwd=str(tmp_path))
    assert unsafe and "scan cap" in refusal
    assert [Path(p) for p in backend.probes] == [host.resolve()]


def test_host_file_big_readable_backend_copy_not_executable_passes(tmp_path):
    host = _local(tmp_path, "status", BIG, mode=0o755)  # host mode must not matter either way
    backend = Backend({str(host): (BIG, False)})
    assert scan(f"D=$(date) && python - <<'PY'\nopen('{host}')\nPY", backend, cwd=str(tmp_path)) == (False, None)
