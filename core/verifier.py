import os
import sys
import shlex
import shutil
from pathlib import Path
from typing import Dict, Any, Optional, Union, List
from core.git_manager import GitManager
from core.process_runner import ProcessTreeRunner, ProcessResult
from core.env_utils import get_sanitized_env

DEFAULT_VERIFICATION_TIMEOUT = 120.0


def verification_timeout(config: Optional[Dict[str, Any]]) -> float:
    """verification.timeout from the Director config, in seconds (invalid or missing -> 120)."""
    try:
        value = float(((config or {}).get("verification") or {}).get("timeout") or DEFAULT_VERIFICATION_TIMEOUT)
    except (TypeError, ValueError):
        return DEFAULT_VERIFICATION_TIMEOUT
    return value if value > 0 else DEFAULT_VERIFICATION_TIMEOUT


class Verifier:
    """Orchestrator-driven verification executing build, test, and git checks."""

    @staticmethod
    def _execute_cmd(cmd_str: Optional[Union[str, List[str]]], cwd: Path, timeout: float = 120.0) -> Dict[str, Any]:
        if not cmd_str:
            return {"command": None, "executed": False, "passed": True, "output": "No command specified"}
        
        try:
            env = get_sanitized_env()
            # Verify under the user's default Python I/O encoding, not whatever the Director process was launched
            # with: an inherited PYTHONIOENCODING/PYTHONUTF8 once hid a CLI that breaks in a normal cp949 terminal.
            for leaked in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO"):
                env.pop(leaked, None)
            cwd_str = str(cwd)
            current_ppath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{cwd_str}{os.pathsep}{current_ppath}" if current_ppath else cwd_str

            if isinstance(cmd_str, list):
                argv = list(cmd_str)
            else:
                raw_tokens = shlex.split(cmd_str, posix=False)
                argv = []
                for t in raw_tokens:
                    if len(t) >= 2 and ((t[0] == '"' and t[-1] == '"') or (t[0] == "'" and t[-1] == "'")):
                        argv.append(t[1:-1])
                    else:
                        argv.append(t)

            if not argv:
                return {"command": cmd_str, "executed": False, "passed": True, "output": "Empty command"}

            # Resolve python to current interpreter if invoked as python/python3
            if argv[0] in ("python", "python3"):
                argv[0] = sys.executable
            else:
                resolved = shutil.which(argv[0])
                if resolved:
                    argv[0] = resolved

            res: ProcessResult = ProcessTreeRunner.run(
                argv=argv,
                cwd=cwd_str,
                env=env,
                timeout=timeout,
                no_output_timeout=None
            )

            if res.timed_out:
                return {
                    "command": cmd_str if isinstance(cmd_str, str) else " ".join(cmd_str),
                    "executed": True,
                    "passed": False,
                    "exit_code": -1,
                    "output": f"Command timed out after {timeout} seconds: {res.timeout_reason}"
                }

            combined_output = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
            passed = (res.exit_code == 0)
            return {
                "command": cmd_str if isinstance(cmd_str, str) else " ".join(cmd_str),
                "executed": True,
                "passed": passed,
                "exit_code": res.exit_code,
                "output": combined_output.strip()
            }
        except Exception as e:
            return {
                "command": cmd_str if isinstance(cmd_str, str) else " ".join(cmd_str),
                "executed": True,
                "passed": False,
                "exit_code": -1,
                "output": f"Execution error: {str(e)}"
            }

    @classmethod
    def verify(
        cls,
        worktree_path: str,
        build_cmd: Optional[str] = None,
        test_cmd: Optional[str] = None,
        lint_cmd: Optional[str] = None,
        timeout: float = 120.0,
        git_checks: bool = True
    ) -> Dict[str, Any]:
        wt = Path(worktree_path).resolve()

        # 1. Git checks
        if git_checks:
            git_status = GitManager.get_status(str(wt))
            git_diff = GitManager.get_diff(str(wt))
        else:                                   # apply-verify on the MAIN tree: never touch its index (no intent-to-add)
            git_status, git_diff = "", ""

        # 2. Run build
        build_res = cls._execute_cmd(build_cmd, wt, timeout=timeout)

        # 3. Run test
        test_res = cls._execute_cmd(test_cmd, wt, timeout=timeout)

        # 4. Run lint
        lint_res = cls._execute_cmd(lint_cmd, wt, timeout=timeout)

        # Overall verification pass condition:
        # Build must pass (if executed)
        # Test must pass (if executed)
        # Lint must pass (if executed)
        overall_passed = build_res["passed"] and test_res["passed"] and lint_res["passed"]
        # `passed` stays True when nothing ran (callers that only need "nothing failed" keep working), but `status`
        # says what actually happened, so evidence never presents an unverified result as verified.
        executed_any = bool(build_res["executed"] or test_res["executed"] or lint_res["executed"])
        status = "NOT_RUN" if not executed_any else ("PASS" if overall_passed else "FAIL")

        summary_parts = []
        if build_res["executed"]:
            summary_parts.append(f"Build: {'PASS' if build_res['passed'] else 'FAIL'}")
        if test_res["executed"]:
            summary_parts.append(f"Test: {'PASS' if test_res['passed'] else 'FAIL'}")
        if lint_res["executed"]:
            summary_parts.append(f"Lint: {'PASS' if lint_res['passed'] else 'FAIL'}")
        
        summary = " | ".join(summary_parts) if summary_parts else "No build/test commands configured"

        # Parse test metrics (e.g. 4 passed, Ran 4 tests)
        test_counts = cls._parse_test_counts(test_res.get("output", ""))

        return {
            "passed": overall_passed,
            "status": status,
            "executed_any": executed_any,
            "summary": summary,
            "build": build_res,
            "test": test_res,
            "lint": lint_res,
            "test_counts": test_counts,
            "git_status": git_status,
            "git_diff": git_diff
        }

    @staticmethod
    def _parse_test_counts(output: str) -> Dict[str, int]:
        import re
        counts = {"passed": 0, "failed": 0, "total": 0}
        if not output:
            return counts

        # Pytest pattern: '12 passed, 1 failed' or '4 passed in 0.02s'
        pass_match = re.search(r"(\d+)\s+passed", output)
        if pass_match:
            counts["passed"] = int(pass_match.group(1))

        fail_match = re.search(r"(\d+)\s+failed", output)
        if fail_match:
            counts["failed"] = int(fail_match.group(1))

        # Unittest pattern: 'Ran 4 tests in 0.001s'
        ran_match = re.search(r"Ran\s+(\d+)\s+tests", output)
        if ran_match:
            counts["total"] = int(ran_match.group(1))
            if "OK" in output:
                counts["passed"] = counts["total"]

        if counts["total"] == 0:
            counts["total"] = counts["passed"] + counts["failed"]

        if counts["total"] == 0:
            counts = Verifier._parse_swift_test_counts(output) or counts

        return counts

    @staticmethod
    def _parse_swift_test_counts(output: str) -> Optional[Dict[str, int]]:
        """`swift test` output: XCTest prints 'Executed N tests, with M failures' per suite (the last line is the
        'All tests' total); Swift Testing prints 'Test run with N tests ... passed|failed ... with K issues'."""
        import re
        total = failed = 0
        found = False
        xctest = re.findall(r"Executed\s+(\d+)\s+tests?,\s+with\s+(\d+)\s+failures?", output)
        if xctest:
            found = True
            total, failed = int(xctest[-1][0]), int(xctest[-1][1])
        for n, outcome, issues in re.findall(
                r"Test run with\s+(\d+)\s+tests?\b[^\n]*?\b(passed|failed)\b(?:[^\n]*?with\s+(\d+)\s+issues?)?", output):
            found = True
            n = int(n)
            total += n
            if outcome == "failed":
                failed += min(n, int(issues or 1))
        if not found:
            return None
        return {"passed": max(total - failed, 0), "failed": failed, "total": total}
