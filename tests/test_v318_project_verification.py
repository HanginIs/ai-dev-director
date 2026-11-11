"""Project-aware verification: the default `python -m pytest tests/` no longer fails every Swift/JS project with
exit 4, an unrelated parent pytest.ini is not adopted, `swift test` counts are read and the per-command timeout and
machine-local config are configurable."""
import json
import os
from pathlib import Path

import director
from core.bootstrap import DEFAULT_TEST_COMMAND, configure_runtime, detect_test_command
from core.verifier import Verifier, verification_timeout


def _cfg(command=DEFAULT_TEST_COMMAND, projects=None):
    return {"verification": {"build_command": None, "test_command": command, "lint_command": None},
            "projects": projects or {}}


def test_swift_package_with_test_targets_uses_swift_test(tmp_path):
    (tmp_path / "Package.swift").write_text('targets: [.target(name: "Core"), .testTarget(name: "CoreTests")]')
    assert detect_test_command(tmp_path) == ["swift", "test"]
    assert configure_runtime(_cfg(), tmp_path)["verification"]["test_command"] == ["swift", "test"]


def test_nothing_detected_runs_nothing_instead_of_a_pytest_that_cannot_pass(tmp_path):
    from core.bootstrap import resolve_test_command
    (tmp_path / "Package.swift").write_text('targets: [.executableTarget(name: "Checks")]')
    assert detect_test_command(tmp_path) is None
    # the project keeps the default; the tree being verified decides (a Builder may add tests/ to a greenfield)
    assert configure_runtime(_cfg(), tmp_path)["verification"]["test_command"] == DEFAULT_TEST_COMMAND
    assert resolve_test_command(DEFAULT_TEST_COMMAND, tmp_path) is None
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_new.py").write_text("def test_x():\n    pass\n")
    assert resolve_test_command(DEFAULT_TEST_COMMAND, tmp_path)[:4] == ["python", "-m", "pytest", "tests/"]
    assert resolve_test_command(["swift", "test"], tmp_path) == ["swift", "test"]   # an explicit command is kept


def test_a_project_runner_script_is_used(tmp_path):
    (tmp_path / "Tests").mkdir()
    (tmp_path / "Tests" / "run.sh").write_text("#!/bin/bash\nswiftc ...\n")
    (tmp_path / "Tests" / "PolicyTests.swift").write_text("// swift\n")
    assert detect_test_command(tmp_path) == ["bash", "Tests/run.sh"]


def test_a_swift_tests_folder_is_never_taken_for_python_tests_on_any_disk(tmp_path):
    (tmp_path / "Tests").mkdir()
    (tmp_path / "Tests" / "test_helper.py").write_text("def test_x():\n    pass\n")   # even with a .py inside
    assert detect_test_command(tmp_path) is None


def test_a_makefile_test_target_is_used(tmp_path):
    (tmp_path / "Makefile").write_text("build:\n\tcc x.c\n\ntest: build\n\t./run-tests\n")
    assert detect_test_command(tmp_path) == ["make", "test"]
    (tmp_path / "Makefile").write_text("build:\n\tcc x.c\n")
    assert detect_test_command(tmp_path) is None


def test_verification_reports_not_run_separately_from_pass(tmp_path):
    from core.verifier import Verifier
    nothing = Verifier.verify(str(tmp_path), git_checks=False)
    assert nothing["status"] == "NOT_RUN" and nothing["executed_any"] is False
    ran = Verifier.verify(str(tmp_path), test_cmd=["python", "-c", "print(1)"], git_checks=False)
    assert ran["status"] == "PASS" and ran["executed_any"] is True
    failed = Verifier.verify(str(tmp_path), test_cmd=["python", "-c", "raise SystemExit(1)"], git_checks=False)
    assert failed["status"] == "FAIL" and failed["passed"] is False


def test_swift_sources_in_tests_folder_are_not_python_tests(tmp_path):
    # macOS disks are case-insensitive: tests/ is the Swift 'Tests' folder there.
    (tmp_path / "tests" / "CoreTests").mkdir(parents=True)
    (tmp_path / "tests" / "CoreTests" / "CoreTests.swift").write_text("import XCTest")
    (tmp_path / "Package.swift").write_text(".testTarget(name: \"CoreTests\")")
    assert detect_test_command(tmp_path) == ["swift", "test"]


def test_python_tests_ignore_parent_config_unless_project_has_its_own(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    assert detect_test_command(tmp_path) == ["python", "-m", "pytest", "tests/", "-c", os.devnull, "--rootdir", "."]
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    assert detect_test_command(tmp_path) == ["python", "-m", "pytest", "tests/"]


def test_pyproject_without_pytest_section_is_not_a_pytest_config(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert "-c" in detect_test_command(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-q'\n")
    assert "-c" not in detect_test_command(tmp_path)


def test_npm_test_script_detected_but_not_the_npm_placeholder(tmp_path):
    package = tmp_path / "package.json"
    package.write_text(json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}))
    assert detect_test_command(tmp_path) is None
    package.write_text(json.dumps({"scripts": {"test": "node --test"}}))
    assert detect_test_command(tmp_path) == ["npm", "test"]
    package.write_text("{not json")
    assert detect_test_command(tmp_path) is None


def test_configured_project_command_wins_over_detection(tmp_path):
    (tmp_path / "Package.swift").write_text(".testTarget(name: \"T\")")
    own = ["sh", "-c", "swift run Checks"]
    cfg = _cfg(projects={str(tmp_path): {"verification": {"test_command": own}}})
    assert configure_runtime(cfg, tmp_path)["verification"]["test_command"] == own
    cfg = _cfg(projects={str(tmp_path): {"verification": {"test_command": DEFAULT_TEST_COMMAND}}})
    assert configure_runtime(cfg, tmp_path)["verification"]["test_command"] == DEFAULT_TEST_COMMAND


def test_detected_python_command_uses_project_venv(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    exe = tmp_path / ".venv" / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.touch()
    got = configure_runtime(_cfg(), tmp_path)["verification"]["test_command"]
    assert got[0] == str(exe.resolve()) and got[1:4] == ["-m", "pytest", "tests/"] and "-c" in got


def test_detected_pytest_command_really_ignores_a_broken_parent_pytest_ini(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --option-that-does-not-exist\n")
    project = tmp_path / "apps" / "app"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    broken = Verifier.verify(str(project), test_cmd=DEFAULT_TEST_COMMAND, git_checks=False)
    assert not broken["passed"]                                   # the parent ini is adopted and breaks pytest
    command = configure_runtime(_cfg(), project)["verification"]["test_command"]
    result = Verifier.verify(str(project), test_cmd=command, git_checks=False)
    assert result["passed"], result["test"]["output"]
    assert result["test_counts"]["passed"] == 1


def test_swift_test_output_counts():
    xctest = ("Test Suite 'EdgeCoreTests' passed at t.\n\t Executed 2 tests, with 0 failures (0 unexpected) in 0.1\n"
              "Test Suite 'All tests' passed at t.\n\t Executed 3 tests, with 0 failures (0 unexpected) in 0.1\n"
              "✔ Test run with 0 tests in 0 suites passed after 0.002 seconds.")
    assert Verifier._parse_test_counts(xctest) == {"passed": 3, "failed": 0, "total": 3}
    failing = "Executed 5 tests, with 2 failures (0 unexpected) in 0.2 seconds"
    assert Verifier._parse_test_counts(failing) == {"passed": 3, "failed": 2, "total": 5}
    testing = "✘ Test run with 4 tests in 2 suites failed after 0.01 seconds with 1 issue."
    assert Verifier._parse_test_counts(testing) == {"passed": 3, "failed": 1, "total": 4}
    assert Verifier._parse_test_counts("12 passed, 1 failed in 0.3s") == {"passed": 12, "failed": 1, "total": 13}
    assert Verifier._parse_test_counts("PASS: render") == {"passed": 0, "failed": 0, "total": 0}


def test_verification_timeout_setting():
    assert verification_timeout({}) == 120.0
    assert verification_timeout({"verification": {"timeout": 300}}) == 300.0
    assert verification_timeout({"verification": {"timeout": "x"}}) == 120.0
    assert verification_timeout({"verification": {"timeout": -5}}) == 120.0


def test_config_local_yaml_is_deep_merged(tmp_path, monkeypatch):
    from core import role_settings
    monkeypatch.setattr(role_settings, "PATH", tmp_path / "no-role-settings.json")
    (tmp_path / "config.yaml").write_text(
        "verification:\n  test_command: python -m pytest tests/\n  timeout: 120\nprojects: {}\n")
    assert director.load_config(tmp_path)["verification"]["timeout"] == 120
    (tmp_path / "config.local.yaml").write_text(
        "verification:\n  timeout: 300\nprojects:\n  /work/app:\n    verification:\n      test_command: [swift, test]\n")
    cfg = director.load_config(tmp_path)
    assert cfg["verification"] == {"test_command": "python -m pytest tests/", "timeout": 300}
    assert cfg["projects"]["/work/app"]["verification"]["test_command"] == ["swift", "test"]


def test_config_local_yaml_is_git_ignored():
    assert "config.local.yaml" in (Path(director.__file__).resolve().parent / ".gitignore").read_text().splitlines()
