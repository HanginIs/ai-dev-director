"""The verifier must execute new source, not a same-timestamp/size old .pyc."""
import os
import py_compile
import sys
from pathlib import Path
from unittest import mock

import pytest

from core.verifier import Verifier


def test_existing_stale_bytecode_is_ignored_without_deleting_it(tmp_path):
    source = tmp_path / "feature.py"
    source.write_text('VALUE = "OLD"\n')
    timestamp = 1_750_000_000
    os.utime(source, (timestamp, timestamp))
    cached = Path(py_compile.compile(str(source), doraise=True))
    old_cache = cached.read_bytes()
    source.write_text('VALUE = "NEW"\n')
    os.utime(source, (timestamp, timestamp))
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    result = Verifier._execute_cmd([sys.executable, "-c", "import feature; print(feature.VALUE)"], tmp_path)
    assert result["passed"], result
    assert result["output"] == "NEW"
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    assert cached.read_bytes() == old_cache


def test_python_children_inherit_isolation(tmp_path):
    source = tmp_path / "feature.py"
    source.write_text('VALUE = "OLD"\n')
    timestamp = 1_750_000_000
    os.utime(source, (timestamp, timestamp))
    py_compile.compile(str(source), doraise=True)
    source.write_text('VALUE = "NEW"\n')
    os.utime(source, (timestamp, timestamp))
    script = "import subprocess,sys; subprocess.run([sys.executable,'-c','import feature; print(feature.VALUE)'],check=True)"
    result = Verifier._execute_cmd([sys.executable, "-c", script], tmp_path)
    assert result["passed"], result
    assert result["output"] == "NEW"


def test_each_verification_gets_a_fresh_prefix_and_cleans_it(tmp_path):
    prefixes = []
    def run(**kwargs):
        path = Path(kwargs["env"]["PYTHONPYCACHEPREFIX"])
        assert path.is_dir()
        prefixes.append(path)
        (path / "cache.pyc").write_bytes(b"private test cache")
        return mock.Mock(timed_out=False, stdout="ok", stderr="", exit_code=0)
    with mock.patch("core.verifier.ProcessTreeRunner.run", side_effect=run):
        assert Verifier._execute_cmd(["python", "probe.py"], tmp_path)["passed"]
        assert Verifier._execute_cmd(["python", "probe.py"], tmp_path)["passed"]
    assert len(set(prefixes)) == 2
    assert all(not p.exists() for p in prefixes)


def test_inherited_cache_and_parent_environment_are_not_modified(tmp_path, monkeypatch):
    user_cache = tmp_path / "operator-cache"
    user_cache.mkdir()
    sentinel = user_cache / "keep.pyc"
    sentinel.write_bytes(b"preserve")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(user_cache))
    def run(**kwargs):
        assert kwargs["env"]["PYTHONPYCACHEPREFIX"] != str(user_cache)
        return mock.Mock(timed_out=False, stdout="ok", stderr="", exit_code=0)
    with mock.patch("core.verifier.ProcessTreeRunner.run", side_effect=run):
        assert Verifier._execute_cmd(["python", "probe.py"], tmp_path)["passed"]
    assert os.environ["PYTHONPYCACHEPREFIX"] == str(user_cache)
    assert sentinel.read_bytes() == b"preserve"


@pytest.mark.parametrize("failure", ["exception", "timeout", "nonzero"])
def test_failure_paths_clean_private_cache_and_remain_failures(tmp_path, failure):
    paths = []
    def run(**kwargs):
        paths.append(Path(kwargs["env"]["PYTHONPYCACHEPREFIX"]))
        if failure == "exception":
            raise RuntimeError("synthetic command error")
        return mock.Mock(timed_out=failure == "timeout", timeout_reason="synthetic timeout",
                         stdout="failed", stderr="", exit_code=1)
    with mock.patch("core.verifier.ProcessTreeRunner.run", side_effect=run):
        result = Verifier._execute_cmd(["python", "probe.py"], tmp_path)
    assert not result["passed"]
    assert len(paths) == 1 and not paths[0].exists()


def test_no_command_creates_no_private_cache(tmp_path):
    with mock.patch("core.verifier.tempfile.TemporaryDirectory") as create:
        assert not Verifier._execute_cmd(None, tmp_path)["executed"]
        assert not Verifier._execute_cmd([], tmp_path)["executed"]
    create.assert_not_called()


def test_failed_assertion_is_not_hidden_by_cache_isolation(tmp_path):
    result = Verifier._execute_cmd([sys.executable, "-c", "assert False, 'must fail'"], tmp_path)
    assert not result["passed"]
    assert "AssertionError: must fail" in result["output"]
