"""
ResultManager — deterministic, human-triggered handling of a PASS result:
view (changed files / diffs / test evidence), open the worktree, APPLY & VERIFY into the main working tree
with backup + rollback, or DISCARD (remove only that run's worktree, keep all evidence).

Hard rules: no merge/commit/push/stash/reset, shell=False everywhere, control-plane paths are never applied,
every target path is canonically re-checked at apply time, nothing touches the main tree without an explicit
human action carrying a request_id, and a failed apply/verify restores the main tree from the backup.
"""
import os
import sys
import json
import hashlib
import shutil
import stat
import subprocess
import tempfile
import threading
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from core.autopilot import only_preexisting_failures
from core.db_path import get_lock_dir
from core.file_lock import LockBusy, advisory_file_lock
from core.state_machine import StateMachine, State
from core.safety import SafetyPolicy
from core.git_manager import GitManager
from core.verifier import Verifier, verification_timeout

RESULT_READY = "READY"
RESULT_APPLIED = "APPLIED"
RESULT_DISCARDED = "DISCARDED"
RESULT_APPLY_FAILED = "APPLY_FAILED_ROLLED_BACK"
RESULT_VERIFY_FAILED = "APPLY_VERIFY_FAILED_ROLLED_BACK"
RESULT_BLOCKED = "APPLY_BLOCKED"
NOTHING_TO_APPLY = "Nothing to apply: the project already has every file of this result"

_APPLY_LOCK = threading.Lock()
_RECENT_APPLY_REQUESTS: Dict[str, str] = {}        # request_id -> status


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _file_identity(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"Not a regular file: {path.name}")
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _file_state(path: Path):
    identity = _file_identity(path)
    if identity is None:
        return None
    digest = _sha(path)
    if _file_identity(path) != identity:
        raise OSError(f"File changed while reading: {path.name}")
    return {"identity": identity, "sha256": digest}


def _check_target(path: Path, expected) -> None:
    if _file_state(path) != expected:
        raise OSError(f"Project file changed since it was checked: {path.name}")


def _replace_file(source: Path, target: Path, expected, digest: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".director-apply-", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        shutil.copy2(source, temporary)
        if _sha(temporary) != digest:
            raise OSError(f"Source changed while copying: {source.name}")
        _check_target(target, expected)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _git(args: List[str], cwd: str) -> Tuple[int, str, str]:
    r = subprocess.run(["git", "-c", "core.quotepath=false"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout, r.stderr


def _project_key(path: str) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except Exception:
        return str(path).lower()


class ResultManager:
    def __init__(self, sm: Optional[StateMachine] = None, config: Optional[dict] = None):
        self.sm = sm or StateMachine()
        self.config = config or {}

    # ------------------------------------------------------------------ artifacts
    @staticmethod
    def run_dir(run_id: str) -> Path:
        from core.db_path import get_runs_dir
        return get_runs_dir() / run_id

    def _load(self, run_id: str, name: str):
        p = self.run_dir(run_id) / name
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def _save(self, run_id: str, name: str, data) -> None:
        d = self.run_dir(run_id); d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def result_state(self, run_id: str) -> Dict[str, Any]:
        st = self._load(run_id, "result_state.json") or {}
        return st if st.get("state") else {"state": RESULT_READY}

    def _set_state(self, run_id: str, state: str, detail: str = "", request_id: str = "", extra: Optional[dict] = None) -> None:
        rec = {"state": state, "at": datetime.utcnow().isoformat(), "detail": detail[:500], "request_id": request_id}
        if extra:
            rec.update(extra)
        self._save(run_id, "result_state.json", rec)
        try:
            self.sm.upsert_result_state(run_id, state=state, detail=detail[:500], request_id=request_id)
        except Exception:
            pass

    def record_summary(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Computes the dashboard summary for a PASS run ONCE (one git status) and persists it to result_state.
        /status then reads the row; no subprocess per poll."""
        run = self.sm.get_run(run_id)
        if not run or run["current_state"] != State.PASS:
            return None
        wt = run.get("worktree_path") or ""
        wt_exists = bool(wt) and Path(wt).exists()
        ev = self._load(run_id, "final_evidence.json") or {}
        ver = self._load(run_id, "verification.json") or {}
        changes = []
        if wt_exists:
            try:
                changes = self.change_set(wt)
            except Exception:
                changes = []
        cp = [c["path"] for c in changes if SafetyPolicy.is_control_plane_path(c["path"])]
        counts = {"A": sum(1 for c in changes if c["type"] == "A"), "M": sum(1 for c in changes if c["type"] == "M"), "D": sum(1 for c in changes if c["type"] == "D")}
        current = self.result_state(run_id).get("state") or RESULT_READY
        fields = {
            "state": current, "changed_count": len(changes), "counts_json": counts,
            "tests_state": "PASS" if ver.get("passed") else "FAIL",
            "reviewer_state": (ev.get("reviewer_verdict") or "").upper() or "UNKNOWN",
            "safety_state": "PASS" if (ev and not ev.get("reasons")) else ("FAIL" if ev else "MISSING"),
            "worktree_state": ("CONTROL_PLANE" if cp else ("PRESENT" if wt_exists else "MISSING")),
        }
        self.sm.upsert_result_state(run_id, **fields)
        self._save(run_id, "result_summary.json", {"run_id": run_id, "changed_files": changes, "control_plane": cp, "counts": counts, "at": datetime.utcnow().isoformat()})
        return fields

    def cached_summary(self, run_id: str) -> Dict[str, Any]:
        """Dashboard summary from persisted state only (DB row + JSON artifacts). No git, no subprocess."""
        run = self.sm.get_run(run_id)
        if not run:
            return {"run_id": run_id, "is_pass": False, "error": "run not found"}
        row = self.sm.get_result_state(run_id)
        if not row and run["current_state"] == State.PASS:
            row = self.record_summary(run_id)                       # first time only (one git status), then cached
            row = self.sm.get_result_state(run_id) or {}
        row = row or {}
        summ = self._load(run_id, "result_summary.json") or {}
        ev = self._load(run_id, "final_evidence.json") or {}
        ver = self._load(run_id, "verification.json") or {}
        base = self._load(run_id, "baseline_verification.json") or {}
        state = row.get("state") or self.result_state(run_id).get("state") or RESULT_READY
        wt = run.get("worktree_path") or ""
        counts = row.get("counts_json")
        if isinstance(counts, str):
            try:
                counts = json.loads(counts)
            except Exception:
                counts = {}
        return {
            "run_id": run_id, "state": run["current_state"], "project_path": run.get("project_path"), "goal": run.get("goal"),
            "worktree_path": wt, "worktree_exists": row.get("worktree_state") in ("PRESENT", "CONTROL_PLANE"),
            "result": {"state": state, "detail": row.get("detail") or "", "request_id": row.get("request_id")},
            "is_pass": run["current_state"] == State.PASS,
            "changed_count": row.get("changed_count") if row.get("changed_count") is not None else len(summ.get("changed_files") or []),
            "counts": counts or {"A": 0, "M": 0, "D": 0}, "control_plane": summ.get("control_plane") or [],
            "changed_files": summ.get("changed_files") or [],
            "tests": {"baseline": base.get("test_counts"), "post": ver.get("test_counts"), "post_passed": ver.get("passed"),
                      "regression": ev.get("regression"), "canonical": ev.get("canonical_containment"),
                      "reviewer": ev.get("reviewer_verdict"), "final_gate": "PASS" if (ev and not ev.get("reasons")) else ("FAIL" if ev else "MISSING")},
        }

    def backfill_missing_summaries(self, limit: int = 500) -> int:
        """One-time (startup) persistence for legacy PASS runs; never called from /status."""
        n = 0
        for r in self.sm.pass_runs_without_result_state(limit):
            try:
                self.record_summary(r["run_id"]); n += 1
            except Exception:
                continue
        return n

    # ------------------------------------------------------------------ change set
    @staticmethod
    def parse_porcelain(text: str) -> List[Dict[str, str]]:
        """git status --porcelain -> [{type: A|M|D|R, path, old_path?}], runtime artifacts excluded."""
        out = []
        for line in (text or "").splitlines():
            if len(line) < 3:
                continue
            if line[2] != " ":                      # GitManager.get_status() strips the first line's leading space
                line = " " + line
            if len(line) < 4:
                continue
            xy, rest = line[:2], line[3:]
            if xy == "??":
                t, path = "A", rest
            elif "R" in xy:
                old, new = [x.strip() for x in rest.split("->", 1)]
                if not SafetyPolicy.is_runtime_artifact(old):
                    out.append({"type": "D", "path": old.strip().strip('"')})
                t, path = "A", new
            elif "D" in xy:
                t, path = "D", rest
            elif "A" in xy:
                t, path = "A", rest
            else:
                t, path = "M", rest
            path = path.strip().strip('"')
            if SafetyPolicy.is_runtime_artifact(path):
                continue
            out.append({"type": t, "path": path})
        # de-duplicate keeping first
        seen, uniq = set(), []
        for c in out:
            key = (c["type"], c["path"])
            if key not in seen:
                seen.add(key); uniq.append(c)
        return uniq

    def change_set(self, worktree_path: str) -> List[Dict[str, str]]:
        rc, out, err = _git(["status", "--porcelain", "--untracked-files=all"], str(Path(worktree_path).resolve()))
        if rc != 0:
            raise RuntimeError(f"git status failed: {err.strip()[:200]}")
        return self.parse_porcelain(out)

    @staticmethod
    def _covers(recorded: set, current: set) -> bool:
        """Recorded evidence may list an untracked directory ('d/') where the live change set lists its files."""
        rec_dirs = {p for (ty, p) in recorded if ty == "A" and p.endswith("/")}
        rec_exact = {(ty, p) for (ty, p) in recorded if not (ty == "A" and p.endswith("/"))}
        cur_exact = set()
        for (ty, p) in current:
            if ty == "A" and any(p.startswith(d) for d in rec_dirs):
                continue
            cur_exact.add((ty, p))
        if cur_exact != rec_exact:
            return False
        return all(any(ty == "A" and p.startswith(d) for (ty, p) in current) for d in rec_dirs)

    def file_diffs(self, worktree_path: str, changes: List[Dict[str, str]]) -> Dict[str, str]:
        full = ""
        try:
            full = GitManager.get_diff(worktree_path)
        except Exception:
            pass
        diffs: Dict[str, str] = {}
        for c in changes:
            f = c["path"]
            marker = f"diff --git a/{f} b/{f}"
            if marker in full:
                seg = full.split(marker, 1)[1]
                nxt = seg.find("\ndiff --git ")
                diffs[f] = (marker + (seg if nxt < 0 else seg[:nxt]))[:20000]
            elif c["type"] == "A":
                p = Path(worktree_path) / f
                if p.is_file():
                    try:
                        diffs[f] = "(new file)\n" + p.read_text(encoding="utf-8", errors="replace")[:20000]
                    except Exception:
                        diffs[f] = "(new binary/unreadable file)"
            elif c["type"] == "D":
                diffs[f] = "(deleted)"
        return diffs

    # ------------------------------------------------------------------ summary for the UI
    def summary(self, run_id: str, with_diffs: bool = False) -> Dict[str, Any]:
        run = self.sm.get_run(run_id)
        if not run:
            return {"error": "run not found", "run_id": run_id}
        wt = run.get("worktree_path") or ""
        ev = self._load(run_id, "final_evidence.json") or {}
        ver = self._load(run_id, "verification.json") or {}
        base = self._load(run_id, "baseline_verification.json") or {}
        order = self._load(run_id, "order.json") or {}
        builder = self._load(run_id, "builder.json") or {}
        prov = (self._load(run_id, "provenance.json") or {}).get("records", [])
        bprov = next((p for p in prov if p.get("agent") == "BUILDER"), {})
        review = self._load(run_id, "review.json") or {}
        approvals = [a for a in self.sm.get_audit_logs(run_id) if a["action"] in ("HUMAN_APPROVAL", "HUMAN_REJECTED")]
        state = self.result_state(run_id)
        changes, diffs, cp = [], {}, []
        wt_exists = bool(wt) and Path(wt).exists()
        if wt_exists and run["current_state"] == State.PASS and state.get("state") != RESULT_DISCARDED:
            try:
                changes = self.change_set(wt)
            except Exception as e:
                changes = []; cp = [f"(change set unavailable: {e})"]
            cp += [c["path"] for c in changes if SafetyPolicy.is_control_plane_path(c["path"])]
            if with_diffs:
                diffs = self.file_diffs(wt, changes)
        counts = {"A": sum(1 for c in changes if c["type"] == "A"), "M": sum(1 for c in changes if c["type"] == "M"), "D": sum(1 for c in changes if c["type"] == "D")}
        test_out = (ver.get("test") or {}).get("output") or ""
        return {
            "run_id": run_id, "state": run["current_state"], "project_path": run.get("project_path"), "goal": run.get("goal"),
            "worktree_path": wt, "worktree_exists": wt_exists, "result": state,
            "is_pass": run["current_state"] == State.PASS,
            "changed_files": changes, "counts": counts, "changed_count": len(changes), "control_plane": cp,
            "diff_stat": [f"{c['type']} {c['path']}" for c in changes], "file_diffs": diffs,
            "tests": {"baseline": base.get("test_counts"), "post": ver.get("test_counts"), "post_passed": ver.get("passed"),
                      "regression": ev.get("regression"), "scope": "PASS" if run["current_state"] == State.PASS else ev.get("order_paths"),
                      "canonical": ev.get("canonical_containment"), "reviewer": ev.get("reviewer_verdict") or review.get("verdict"),
                      "final_gate": "PASS" if (ev and not ev.get("reasons")) else ("FAIL" if ev else "MISSING"),
                      "verifier_output_tail": test_out[-1500:], "verifier_summary": ver.get("summary")},
            "final_evidence": ev, "reviewer": {"verdict": review.get("verdict"), "summary": review.get("summary"), "p0": review.get("p0"), "p1": review.get("p1")},
            "builder": {"provider": bprov.get("provider"), "version": bprov.get("version"), "exit_code": bprov.get("exit_code"), "status": builder.get("status")},
            "order": {k: order.get(k) for k in ("goal", "allowed_paths", "protected_paths", "allowed_deletions", "risk_level")},
            "approvals": [{"action": a["action"], "at": a["timestamp"], "payload": (a["payload"] or "")[:300]} for a in approvals],
        }

    def pending_results(self) -> List[Dict[str, Any]]:
        """READY results from the persisted result_state table only (no run scan, no git)."""
        out = []
        for r in self.sm.list_pending_results():
            if str(r.get("run_id", "")).startswith("RUN-TEST-"):
                continue
            proj = r.get("project_path") or ""
            out.append({"run_id": r["run_id"], "project_name": Path(proj).name if proj else "", "project_path": proj,
                        "changed_count": r.get("changed_count"), "result_state": r.get("state") or RESULT_READY, "created_at": r.get("created_at")})
        return out

    # ------------------------------------------------------------------ containment helpers
    @staticmethod
    def worktree_contained(project_path: str, worktree_path: str) -> bool:
        try:
            proj = Path(project_path).resolve(); wt = Path(worktree_path).resolve()
            return SafetyPolicy.is_within_root(proj / ".director_worktrees", wt) and wt != (proj / ".director_worktrees").resolve()
        except Exception:
            return False

    # ------------------------------------------------------------------ open in Explorer
    def open_worktree(self, run_id: str) -> Dict[str, Any]:
        run = self.sm.get_run(run_id)
        if not run or not run.get("worktree_path"):
            return {"status": "refused", "reason": "no worktree recorded"}
        wt, proj = run["worktree_path"], run.get("project_path") or ""
        if not Path(wt).exists() or not self.worktree_contained(proj, wt):
            return {"status": "refused", "reason": "worktree missing or not inside the project's .director_worktrees"}
        return self._open(str(Path(wt).resolve()))

    def open_project(self, run_id: str) -> Dict[str, Any]:
        run = self.sm.get_run(run_id)
        proj = (run or {}).get("project_path") or ""
        if not proj or not Path(proj).exists() or not (Path(proj) / ".git").exists():
            return {"status": "refused", "reason": "project missing"}
        return self._open(str(Path(proj).resolve()))

    @staticmethod
    def _open(path: str) -> Dict[str, Any]:
        argv = ["explorer.exe", path] if os.name == "nt" else (["open", path] if sys.platform == "darwin" else ["xdg-open", path])
        try:
            subprocess.Popen(argv)                                   # argv, shell=False
        except OSError as exc:
            return {"status": "refused", "reason": f"file manager unavailable: {exc}"}
        return {"status": "ok", "path": path}

    # ------------------------------------------------------------------ preconditions + manifest
    def _evidence_block(self, run_id: str, allow_not_run: bool) -> Optional[str]:
        """Why the run's evidence does not allow applying it, or None. A run nothing was built or tested for
        (verification NOT_RUN) is only applied by a person who chooses to, never automatically."""
        ev = self._load(run_id, "final_evidence.json") or {}
        if ev.get("verification") == "NOT_RUN" and not ev.get("reasons") and ev.get("reviewer_verdict") == "pass" \
                and ev.get("canonical_containment") == "PASS":
            return None if allow_not_run else ("No build or test command ran for this result (verification NOT_RUN); "
                                               "it is not applied automatically")
        return None if self._delivery_evidence_passes(run_id) else "final_evidence is missing or not fully PASS"

    def _delivery_evidence_passes(self, run_id: str) -> bool:
        ev = self._load(run_id, "final_evidence.json") or {}
        if (ev.get("reasons") or ev.get("canonical_containment") != "PASS"
                or ev.get("verification") != "PASS" or ev.get("regression") != "PASS"
                or ev.get("reviewer_verdict") != "pass"):
            return False
        if ev.get("baseline") == "PASS":
            return True
        baseline = self._load(run_id, "baseline_verification.json") or {}
        post = self._load(run_id, "verification.json") or {}
        return (ev.get("baseline") == "PREEXISTING_FAILURES" and baseline.get("passed") is False
                and post.get("passed") is True)

    def _result_is_final(self, run_id: str) -> bool:
        """APPLIED and DISCARDED are final. A repeat request is refused and logged, but it must not rewrite the
        state: an applied result once showed as APPLY_BLOCKED after a second click."""
        return self.result_state(run_id).get("state") in (RESULT_APPLIED, RESULT_DISCARDED)

    def result_preconditions(self, run_id: str) -> Dict[str, Any]:
        """What the console shows before a person applies one run (a person may apply an unverified result)."""
        if self._load(run_id, "source_snapshot.json") is not None:
            return self.tree_preconditions(run_id, cumulative=False, allow_not_run=True)
        return self.preconditions(run_id, allow_not_run=True)

    def preconditions(self, run_id: str, allow_not_run: bool = False) -> Dict[str, Any]:
        blocks: List[str] = []
        run = self.sm.get_run(run_id)
        if not run:
            return {"ok": False, "blocks": ["run not found"], "manifest": []}
        proj, wt = run.get("project_path") or "", run.get("worktree_path") or ""
        if run["current_state"] != State.PASS:
            blocks.append(f"Run state is {run['current_state']}, not PASS")
        st = self.result_state(run_id).get("state")
        if st in (RESULT_APPLIED, RESULT_DISCARDED):
            blocks.append(f"Result already {st}")
        evidence_block = self._evidence_block(run_id, allow_not_run)
        if evidence_block:
            blocks.append(evidence_block)
        if not wt or not Path(wt).exists():
            blocks.append("Worktree missing")
        if not proj or not Path(proj).exists() or not (Path(proj) / ".git").exists():
            blocks.append("Project missing")
        if wt and proj and Path(wt).exists() and Path(proj).exists() and not self.worktree_contained(proj, wt):
            blocks.append("Worktree canonical containment failed")
        manifest: List[Dict[str, Any]] = []
        if not blocks:
            blocks += self.review_drift(run_id, wt)
        if not blocks:
            order = self._load(run_id, "order.json") or {}
            changes = self.change_set(wt)
            ver_status = (self._load(run_id, "verification.json") or {}).get("git_status") or ""
            recorded = {(c["type"], c["path"]) for c in self.parse_porcelain(ver_status)}
            current = {(c["type"], c["path"]) for c in changes}
            if not self._covers(recorded, current):
                blocks.append(f"Change set differs from verification evidence (recorded={sorted(recorded)} now={sorted(current)})")
            builder_files = set((self._load(run_id, "builder.json") or {}).get("files_modified") or [])
            wt_root = Path(wt).resolve(); proj_root = Path(proj).resolve()
            allowed_del = set(order.get("allowed_deletions") or [])
            for c in changes:
                rel = c["path"]
                if SafetyPolicy.is_control_plane_path(rel):
                    blocks.append(f"Control-plane file in result: {rel}")
                    continue
                ok, why = SafetyPolicy.is_repo_relative_path(rel)
                if not ok:
                    blocks.append(f"Invalid result path: {rel} ({why})"); continue
                src = wt_root / rel; dst = proj_root / rel
                if c["type"] != "D":
                    if not src.is_file() or not SafetyPolicy.is_within_root(wt_root, src):
                        blocks.append(f"Result file missing or escapes worktree: {rel}"); continue
                    if dst.exists() and not SafetyPolicy.is_within_root(proj_root, dst):
                        blocks.append(f"Target escapes project (symlink/junction): {rel}"); continue
                    # any existing parent that is a link escaping the project
                    parent = dst.parent
                    if parent.exists() and not SafetyPolicy.is_within_root(proj_root, parent):
                        blocks.append(f"Target parent escapes project: {rel}"); continue
                    entry = {"path": rel, "type": c["type"], "source": str(src), "sha256": _sha(src), "size": src.stat().st_size}
                    if c["type"] == "A" and dst.exists():
                        blocks.append(f"Untracked/existing file collides with result: {rel}")
                else:
                    if rel not in allowed_del:
                        blocks.append(f"Unauthorized deletion in result: {rel}"); continue
                    entry = {"path": rel, "type": "D", "source": None, "sha256": None, "size": 0, "deletion": True}
                try:
                    entry["target_before"] = _file_state(dst)
                except OSError as exc:
                    blocks.append(f"Cannot check target {rel}: {exc}")
                    continue
                manifest.append(entry)
            if builder_files and not builder_files.issubset({c["path"] for c in changes} | {b for b in builder_files if b not in {c["path"] for c in changes} and not (wt_root / b).exists()}):
                pass  # builder may report files it later left unchanged; the git change set is the source of truth
            # main repo state
            rc, wt_head, _ = _git(["rev-parse", "HEAD"], wt)
            rc2, main_head, _ = _git(["rev-parse", "HEAD"], proj)
            if rc or rc2 or not wt_head.strip() or not main_head.strip():
                blocks.append("Cannot read source/base commit")
            elif wt_head.strip() != main_head.strip():
                blocks.append("Main project changed since this run was created (HEAD differs from the run's base commit)")
            rc3, status, _ = _git(["status", "--porcelain", "--untracked-files=no"], proj)
            if rc3:
                blocks.append("Cannot read main working tree status")
            elif status.strip():
                blocks.append("Working tree is not clean (tracked changes present)")
        return {"ok": not blocks, "blocks": blocks, "manifest": manifest, "project_path": proj, "worktree_path": wt,
                "base_commit": (_git(["rev-parse", "HEAD"], wt)[1].strip() if wt and Path(wt).exists() else None)}

    # ------------------------------------------------------------------ apply & verify (human action)
    def apply(self, run_id: str, request_id: str = "", applied_by: str = "HUMAN", test_cmd: Optional[str] = None) -> Dict[str, Any]:
        return self._apply_request(run_id, request_id, applied_by, test_cmd=test_cmd)

    def _apply_request(self, run_id: str, request_id: str, applied_by: str, *,
                       cumulative: Optional[bool] = None, on_conflict: str = "block",
                       test_cmd: Optional[str] = None) -> Dict[str, Any]:
        rid = (request_id or "").strip() or hashlib.sha256(f"{run_id}{datetime.utcnow().isoformat()}".encode()).hexdigest()[:16]
        with _APPLY_LOCK:
            if rid in _RECENT_APPLY_REQUESTS:
                return {"status": "duplicate", "run_id": run_id, "request_id": rid}
            _RECENT_APPLY_REQUESTS[rid] = "inflight"
            while len(_RECENT_APPLY_REQUESTS) > 500:
                _RECENT_APPLY_REQUESTS.pop(next(iter(_RECENT_APPLY_REQUESTS)))
        retryable = False
        try:
            project = (self.sm.get_run(run_id) or {}).get("project_path")
            lock = nullcontext()
            if project:
                key = hashlib.sha256(_project_key(project).encode("utf-8")).hexdigest()
                lock = advisory_file_lock(get_lock_dir() / f"apply-{key}.lock")
            with lock:
                return self._apply_checked(run_id, rid, applied_by, cumulative, on_conflict, test_cmd)
        except LockBusy:
            retryable = True
            return {"status": "busy", "run_id": run_id, "message": "another apply is in progress for this project"}
        finally:
            with _APPLY_LOCK:
                if retryable:
                    _RECENT_APPLY_REQUESTS.pop(rid, None)
                elif _RECENT_APPLY_REQUESTS.get(rid) == "inflight":
                    _RECENT_APPLY_REQUESTS[rid] = "done"

    def _apply_checked(self, run_id: str, rid: str, applied_by: str, cumulative: Optional[bool],
                       on_conflict: str, test_cmd: Optional[str]) -> Dict[str, Any]:
        allow_not_run = applied_by == "HUMAN"
        if cumulative is None:
            pre = self.preconditions(run_id, allow_not_run=allow_not_run)
        else:
            pre = self.tree_preconditions(run_id, on_conflict=on_conflict, cumulative=cumulative,
                                          allow_not_run=allow_not_run)
            if pre["blocks"] == [NOTHING_TO_APPLY]:
                self.sm.log_audit(run_id, applied_by, "RESULT_NOTHING_TO_APPLY", json.dumps({"request_id": rid}))
                return {"status": "nothing_to_apply", "run_id": run_id, "files": [], "request_id": rid}
        if not pre["ok"]:
            if not pre.get("previous_run_applied") and not self._result_is_final(run_id):
                self._set_state(run_id, RESULT_BLOCKED, "; ".join(pre["blocks"]), rid)
            self.sm.log_audit(run_id, applied_by, "RESULT_APPLY_BLOCKED", json.dumps(
                {"blocks": pre["blocks"], "request_id": rid}, ensure_ascii=False))
            result = {"status": "blocked", "run_id": run_id, "blocks": pre["blocks"], "request_id": rid}
            if cumulative is not None:
                result["conflicts"] = pre["conflicts"]
            return result
        if cumulative is None:
            return self._apply_locked(run_id, pre, rid, applied_by, test_cmd)
        from core.bootstrap import configure_runtime
        self.config = configure_runtime(self.config, pre["project_path"])
        result = self._apply_locked(run_id, pre, rid, applied_by, None)
        result["skipped_conflicts"] = pre["conflicts"]
        result["deleted_not_applied"] = pre["deleted"]
        return result

    def _apply_locked(self, run_id: str, pre: Dict[str, Any], rid: str, applied_by: str, test_cmd: Optional[str]) -> Dict[str, Any]:
        proj_root = Path(pre["project_path"]).resolve()
        manifest = pre["manifest"]
        self._save(run_id, "result_manifest.json", {"run_id": run_id, "base_commit": pre["base_commit"], "files": manifest, "created_at": datetime.utcnow().isoformat()})
        self.sm.log_audit(run_id, applied_by, "RESULT_APPLY_STARTED", json.dumps(
            {"run_id": run_id, "project": str(proj_root), "files": [m["path"] for m in manifest], "hashes": {m["path"]: m["sha256"] for m in manifest},
             "source_commit": pre["base_commit"], "applied_by": applied_by, "timestamp": datetime.utcnow().isoformat(), "request_id": rid}, ensure_ascii=False))
        backup_dir = self.run_dir(run_id) / "apply_backup"
        backup_manifest = []
        applied: Dict[str, Any] = {}
        def rollback(reason: str, state: str) -> Dict[str, Any]:
            errors = []
            conflicts = []
            for e in backup_manifest:
                if e["path"] not in applied:
                    continue
                dst = proj_root / e["path"]
                try:
                    expected = applied[e["path"]]
                    if not SafetyPolicy.is_within_root(proj_root, dst) or _file_state(dst) != expected:
                        conflicts.append(e["path"])
                        continue
                    if e["existed"]:
                        _replace_file(backup_dir / e["path"], dst, expected, e["sha256"])
                    elif expected is not None:
                        _check_target(dst, expected)
                        dst.unlink()
                except Exception as ex:
                    errors.append(f"{e['path']}: {ex}")
            if conflicts:
                reason += "; later project edits preserved: " + ", ".join(conflicts)
            details = {"rollback_errors": errors, "rollback_conflicts": conflicts}
            if not pre.get("previous_run_applied") or errors or conflicts:
                self._set_state(run_id, state, reason, rid, details)
            self.sm.log_audit(run_id, applied_by, "RESULT_APPLY_ROLLED_BACK", json.dumps(
                {"reason": reason[:500], **details, "request_id": rid}, ensure_ascii=False))
            return {"status": "rolled_back", "run_id": run_id, "reason": reason, **details, "result_state": state, "request_id": rid}

        try:
            # Check all targets before replacing any file or replacing the previous backup.
            for m in manifest:
                dst = proj_root / m["path"]
                if not SafetyPolicy.is_within_root(proj_root, dst):
                    raise OSError(f"Target escapes project: {m['path']}")
                _check_target(dst, m["target_before"])
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            backup_dir.mkdir(parents=True)
            for m in manifest:
                dst = proj_root / m["path"]
                before = m["target_before"]
                if before is not None:
                    bp = backup_dir / m["path"]
                    bp.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, bp)
                    if _sha(bp) != before["sha256"]:
                        raise OSError(f"Project file changed during backup: {m['path']}")
                _check_target(dst, before)
                backup_manifest.append({"path": m["path"], "existed": before is not None,
                                        "sha256": before["sha256"] if before is not None else None})
            (backup_dir / "backup_manifest.json").write_text(json.dumps(backup_manifest, indent=2), encoding="utf-8")

            for m in manifest:
                rel = m["path"]; dst = proj_root / rel
                if SafetyPolicy.is_control_plane_path(rel):
                    raise RuntimeError(f"control-plane path refused at apply time: {rel}")
                if dst.parent.exists() and not SafetyPolicy.is_within_root(proj_root, dst.parent):
                    raise RuntimeError(f"target parent escapes project at apply time: {rel}")
                if dst.exists() and not SafetyPolicy.is_within_root(proj_root, dst):
                    raise RuntimeError(f"target escapes project at apply time: {rel}")
                if m.get("deletion"):
                    _check_target(dst, m["target_before"])
                    dst.unlink()
                    applied[rel] = None
                    continue
                src = Path(m["source"])
                dst.parent.mkdir(parents=True, exist_ok=True)
                _replace_file(src, dst, m["target_before"], m["sha256"])
                written = _file_state(dst)
                if written is None or written["sha256"] != m["sha256"]:
                    raise RuntimeError(f"hash mismatch after write: {rel}")
                applied[rel] = written
        except Exception as e:
            return rollback(f"APPLY_FAILED: {e}", RESULT_APPLY_FAILED)

        try:
            from core.bootstrap import resolve_test_command
            v = self.config.get("verification", {}) or {}
            cmd = resolve_test_command(test_cmd if test_cmd is not None else v.get("test_command"), str(proj_root))
            verification = Verifier.verify(worktree_path=str(proj_root), build_cmd=v.get("build_command"),
                                           test_cmd=cmd, lint_cmd=v.get("lint_command"), git_checks=False,
                                           timeout=verification_timeout(self.config))
            post = self._load(run_id, "verification.json") or {}
            evidence = self._load(run_id, "final_evidence.json") or {}
            if not verification.get("passed") and evidence.get("baseline") == "PREEXISTING_FAILURES":
                baseline = self._load(run_id, "baseline_verification.json")
                unchanged = only_preexisting_failures(baseline, verification)
                if unchanged and unchanged.issubset(post.get("preexisting_failures") or []):
                    verification["passed"] = True
                    verification["preexisting_failures"] = sorted(unchanged)
                    verification["summary"] += "; PRE-EXISTING FAILURES UNCHANGED"
            self._save(run_id, "apply_verification.json", verification)
            expected = int((post.get("test_counts") or {}).get("passed", 0) or 0)
            got = int((verification.get("test_counts") or {}).get("passed", 0) or 0)
            if not verification.get("passed") or got < expected:
                raise RuntimeError(f"passed={verification.get('passed')} tests={got} expected>={expected}: {verification.get('summary')}")
            for rel, written in applied.items():
                _check_target(proj_root / rel, written)
        except Exception as exc:
            return rollback(f"APPLY_VERIFY_FAILED: {exc}", RESULT_VERIFY_FAILED)
        self._set_state(run_id, RESULT_APPLIED, f"{len(manifest)} files applied and verified ({got} tests passed)", rid,
                        {"files": [m["path"] for m in manifest], "verification": verification.get("test_counts"),
                         "apply_scope": pre.get("apply_scope")})
        self.sm.log_audit(run_id, applied_by, "RESULT_APPLY_COMPLETED", json.dumps(
            {"run_id": run_id, "project": str(proj_root), "files": [m["path"] for m in manifest], "hashes": {m["path"]: m["sha256"] for m in manifest},
             "source_commit": pre["base_commit"], "applied_by": applied_by, "timestamp": datetime.utcnow().isoformat(),
             "verification": {"passed": verification.get("passed"), "test_counts": verification.get("test_counts")}, "request_id": rid}, ensure_ascii=False))
        return {"status": "applied", "run_id": run_id, "files": [m["path"] for m in manifest], "verification": verification.get("test_counts"), "request_id": rid}

    # ------------------------------------------------------------------ mission-aware delivery (v3.2)
    # A milestone worktree is checked out at a SNAPSHOT commit (the project's files, untracked ones included, or the
    # previous milestone's worktree), so "git status" in it only shows this run's own edits and its HEAD never equals
    # the project's HEAD. The legacy apply() above therefore blocked every mission result ("HEAD differs", "untracked
    # file collides"). Delivery compares the finished worktree with the project file by file instead: a file is
    # applied when the AIs changed it and the user did not touch it since the chain of milestones began.
    GENERATED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", "node_modules", ".cache", ".ipynb_checkpoints"}
    GENERATED_SUFFIXES = (".pyc", ".pyo", ".pyd", ".log", ".tmp", ".swp")
    GENERATED_NAMES = {".DS_Store", "Thumbs.db"}

    @classmethod
    def is_generated_path(cls, rel: str) -> bool:
        """Bytecode, tool caches and editor droppings: produced by running the project, not written by the AIs.
        They are excluded from the delivery manifest so a cache rewritten during apply-verification cannot
        trigger a rollback of a good deliverable (MSN-261029080201 was lost to src/__pycache__/*.pyc)."""
        parts = rel.replace("\\", "/").split("/")
        if any(part in cls.GENERATED_DIRS for part in parts[:-1]):
            return True
        name = parts[-1]
        return name in cls.GENERATED_NAMES or name.lower().endswith(cls.GENERATED_SUFFIXES)

    @staticmethod
    def _blob_id(data: bytes) -> str:
        return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

    def origin_ref(self, run_id: str) -> Optional[str]:
        """The project snapshot the chain of milestones started from (a run built on an earlier worktree carries that
        worktree's snapshot, so follow the chain back to the first run)."""
        seen, rid = set(), run_id
        while rid and rid not in seen:
            seen.add(rid)
            snap = self._load(rid, "source_snapshot.json") or {}
            base = snap.get("base_worktree") or (self._load(rid, "mission_base.json") or {}).get("base_worktree")
            if not base:
                return snap.get("base_ref")
            rid = Path(base).name                          # a worktree is named after its run
        return None

    # ------------------------------------------------------------------ what was reviewed is what gets applied
    # When a run reaches PASS the Director records every file of its worktree (git blob ids). Applying refuses a
    # worktree that no longer matches that record: a file edited, added or removed after the review was never
    # reviewed. A run without the record (made before this check existed) has to be reviewed again.
    REVIEW_RECORD = "reviewed_tree.json"

    def _fingerprint_paths(self, wt_root: Path) -> Optional[List[str]]:
        rc1, tracked, _ = _git(["ls-files", "-z"], str(wt_root))
        rc2, others, _ = _git(["ls-files", "--others", "--exclude-standard", "-z"], str(wt_root))
        if rc1 or rc2:
            return None
        paths = []
        for rel in dict.fromkeys(filter(None, tracked.split("\0") + others.split("\0"))):
            rel = rel.replace("\\", "/")
            if SafetyPolicy.is_control_plane_path(rel) or rel.startswith(".director_worktrees/") or self.is_generated_path(rel):
                continue
            path = wt_root / rel
            if path.is_file() and not path.is_symlink():
                paths.append(rel)
        return sorted(paths)

    def worktree_fingerprint(self, worktree_path: str) -> Optional[Dict[str, str]]:
        """{path: git blob id} for every deliverable file of the worktree, or None when it cannot be read."""
        wt_root = Path(worktree_path).resolve()
        if not wt_root.is_dir():
            return None
        paths = self._fingerprint_paths(wt_root)
        if paths is None:
            return None
        ids = self._git_blob_ids(wt_root, paths)
        if len(ids) != len(paths):
            return None
        return ids

    def record_review(self, run_id: str, worktree_path: Optional[str] = None) -> bool:
        """Remember exactly which files the final review passed. Called once, when the run reaches PASS."""
        wt = worktree_path or (self.sm.get_run(run_id) or {}).get("worktree_path") or ""
        files = self.worktree_fingerprint(wt) if wt else None
        if files is None:
            return False
        rc, head, _ = _git(["rev-parse", "HEAD"], str(Path(wt).resolve()))
        self._save(run_id, self.REVIEW_RECORD, {"run_id": run_id, "head": head.strip() if rc == 0 else None,
                                                 "files": files, "recorded_at": datetime.utcnow().isoformat()})
        return True

    def review_drift(self, run_id: str, worktree_path: str) -> List[str]:
        """Blocks when the worktree is not exactly what the final review passed (empty list when it is)."""
        record = self._load(run_id, self.REVIEW_RECORD)
        if not isinstance(record, dict) or not isinstance(record.get("files"), dict):
            return ["No record of the reviewed files for this run; review it again before applying"]
        current = self.worktree_fingerprint(worktree_path)
        if current is None:
            return ["Cannot read the worktree files to compare them with the review"]
        reviewed = record["files"]
        changed = sorted(p for p in set(current) & set(reviewed) if current[p] != reviewed[p])
        added = sorted(set(current) - set(reviewed))
        removed = sorted(set(reviewed) - set(current))
        if not (changed or added or removed):
            return []
        parts = [f"{label}: {', '.join(paths[:8])}{' ...' if len(paths) > 8 else ''}"
                 for label, paths in (("changed", changed), ("added", added), ("removed", removed)) if paths]
        return ["Worktree changed after the review (" + "; ".join(parts) + "); review it again before applying"]

    @staticmethod
    def _git_blob_ids(root: Path, rels: List[str]) -> Dict[str, str]:
        """Blob ids exactly as git would store these working files (line-ending conversion included), in one call."""
        if not rels:
            return {}
        r = subprocess.run(["git", "-c", "core.quotepath=false", "hash-object", "--stdin-paths"], cwd=str(root),
                           input="\n".join(rels) + "\n", capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode:
            return {}
        return dict(zip(rels, r.stdout.split()))

    def tree_manifest(self, run_id: str, *, cumulative: bool = True) -> Dict[str, Any]:
        """Every file of the finished worktree that differs from the project, classified: A (new), M (changed by the
        AIs, untouched by the user), CONFLICT (both changed), and deletions the AIs made (reported, never applied).
        Comparisons use git's own blob ids, so CRLF conversion on Windows cannot fake a difference."""
        run = self.sm.get_run(run_id) or {}
        wt, proj = run.get("worktree_path") or "", run.get("project_path") or ""
        wt_root, proj_root = Path(wt).resolve(), Path(proj).resolve()
        snapshot = self._load(run_id, "source_snapshot.json") or {}
        origin = self.origin_ref(run_id) if cumulative else snapshot.get("base_ref")
        if not cumulative:
            rc, head, _ = _git(["rev-parse", "HEAD"], str(wt_root))
            if not origin or rc or head.strip() != origin:
                return {"ok": False, "blocks": ["Run worktree no longer matches its recorded base"],
                        "manifest": [], "conflicts": [], "deleted": []}
            post = self._load(run_id, "verification.json") or {}
            if "git_status" in post:
                recorded = {(c["type"], c["path"]) for c in self.parse_porcelain(post["git_status"] or "")
                            if not self.is_generated_path(c["path"])}
                current = {(c["type"], c["path"]) for c in self.change_set(str(wt_root))
                           if not self.is_generated_path(c["path"])}
                if not self._covers(recorded, current):
                    return {"ok": False, "blocks": ["Run changes differ from verification evidence"],
                            "manifest": [], "conflicts": [], "deleted": []}
        origin_blobs: Dict[str, str] = {}
        if origin:
            rc, out, _ = _git(["ls-tree", "-r", "-z", origin], str(proj_root))
            if rc and not cumulative:
                return {"ok": False, "blocks": ["Cannot read the run's recorded base tree"],
                        "manifest": [], "conflicts": [], "deleted": []}
            if rc == 0:
                for rec in out.split("\0"):
                    if "\t" in rec:
                        meta, rel = rec.split("\t", 1)
                        origin_blobs[rel.replace("\\", "/")] = meta.split()[2]
        rc1, tracked, _ = _git(["ls-files", "-z"], str(wt_root))
        rc2, others, _ = _git(["ls-files", "--others", "--exclude-standard", "-z"], str(wt_root))
        if rc1 or rc2:
            return {"ok": False, "blocks": ["cannot list worktree files"], "manifest": [], "conflicts": [], "deleted": []}
        candidates, conflicts = [], []
        target_identities = {}
        for rel in dict.fromkeys(filter(None, tracked.split("\0") + others.split("\0"))):
            rel = rel.replace("\\", "/")
            if SafetyPolicy.is_control_plane_path(rel) or rel.startswith(".director_worktrees/"):
                continue
            if self.is_generated_path(rel):
                continue                                     # caches/bytecode are never deliverables (2026-10-29 rollback cause)
            ok, _why = SafetyPolicy.is_repo_relative_path(rel)
            src, dst = wt_root / rel, proj_root / rel
            if not ok or not src.is_file() or src.is_symlink() or not SafetyPolicy.is_within_root(wt_root, src):
                continue
            if dst.exists() and (not SafetyPolicy.is_within_root(proj_root, dst) or (dst.parent.exists() and not SafetyPolicy.is_within_root(proj_root, dst.parent))):
                conflicts.append({"path": rel, "reason": "target escapes project"}); continue
            try:
                target_identities[rel] = _file_identity(dst)
            except OSError as exc:
                conflicts.append({"path": rel, "reason": str(exc)})
                continue
            candidates.append(rel)
        dst_paths = [rel for rel in candidates if target_identities[rel] is not None]
        src_ids = self._git_blob_ids(wt_root, candidates)
        dst_ids = self._git_blob_ids(proj_root, dst_paths)
        if len(src_ids) != len(candidates) or len(dst_ids) != len(dst_paths):
            return {"ok": False, "blocks": ["Cannot compare run and project files"],
                    "manifest": [], "conflicts": [], "deleted": []}
        manifest, unchanged = [], 0
        for rel in candidates:
            src, dst = wt_root / rel, proj_root / rel
            src_blob, dst_blob, origin_blob = src_ids.get(rel), dst_ids.get(rel), origin_blobs.get(rel)
            if dst_blob is not None:
                if dst_blob == src_blob or src_blob == origin_blob:
                    unchanged += 1; continue                 # same already, or the AIs left it alone (user's version stays)
                if dst_blob != origin_blob:
                    conflicts.append({"path": rel, "reason": "changed in the project since the work started"}); continue
                kind = "M"
            else:
                if origin_blob is not None and src_blob == origin_blob:
                    unchanged += 1; continue                 # the user deleted it meanwhile; not resurrected
                if origin_blob is not None:
                    # The user deleted a file the AIs changed. Writing it back would undo the user's deletion, so it
                    # is a conflict for a single run and for a whole mission alike.
                    conflicts.append({"path": rel, "reason": "removed in the project since the work started"}); continue
                kind = "A"
            try:
                target_before = _file_state(dst)
                if (target_before or {}).get("identity") != target_identities[rel]:
                    raise OSError("changed while comparing project files")
            except OSError as exc:
                conflicts.append({"path": rel, "reason": str(exc)})
                continue
            data = src.read_bytes()
            manifest.append({"path": rel, "type": kind, "source": str(src), "sha256": hashlib.sha256(data).hexdigest(),
                             "size": len(data), "target_before": target_before})
        present = set(candidates)
        deleted = [rel for rel in origin_blobs if rel not in present and (proj_root / rel).exists()
                   and not SafetyPolicy.is_control_plane_path(rel)]
        return {"ok": True, "blocks": [], "manifest": manifest, "conflicts": conflicts, "deleted": deleted,
                "unchanged": unchanged, "origin_ref": origin}

    def tree_preconditions(self, run_id: str, on_conflict: str = "block", *, cumulative: bool = True,
                           allow_not_run: bool = False) -> Dict[str, Any]:
        blocks: List[str] = []
        run = self.sm.get_run(run_id)
        if not run:
            return {"ok": False, "blocks": ["run not found"], "manifest": [], "conflicts": []}
        proj, wt = run.get("project_path") or "", run.get("worktree_path") or ""
        if run["current_state"] != State.PASS:
            blocks.append(f"Run state is {run['current_state']}, not PASS")
        result = self.result_state(run_id)
        st = result.get("state")
        previous_run_applied = cumulative and st == RESULT_APPLIED and result.get("apply_scope") == "run"
        if st in (RESULT_APPLIED, RESULT_DISCARDED) and not previous_run_applied:
            blocks.append(f"Result already {st}")
        evidence_block = self._evidence_block(run_id, allow_not_run)
        if evidence_block:
            blocks.append(evidence_block)
        if not wt or not Path(wt).exists():
            blocks.append("Worktree missing")
        if not proj or not Path(proj).exists() or not (Path(proj) / ".git").exists():
            blocks.append("Project missing")
        if wt and proj and Path(wt).exists() and Path(proj).exists() and not self.worktree_contained(proj, wt):
            blocks.append("Worktree canonical containment failed")
        if not blocks:
            blocks += self.review_drift(run_id, wt)
        tree = self.tree_manifest(run_id, cumulative=cumulative) if not blocks else {"manifest": [], "conflicts": [], "deleted": [], "blocks": [], "origin_ref": None}
        blocks += tree.get("blocks", [])
        if tree["conflicts"] and on_conflict == "block":
            blocks.append("Conflicts: " + ", ".join(c["path"] for c in tree["conflicts"][:12]) + (" ..." if len(tree["conflicts"]) > 12 else ""))
        if not blocks and not tree["manifest"]:
            blocks.append(NOTHING_TO_APPLY)
        return {"ok": not blocks, "blocks": blocks, "manifest": tree["manifest"], "conflicts": tree["conflicts"],
                "deleted": tree.get("deleted", []), "unchanged": tree.get("unchanged", 0), "project_path": proj,
                "worktree_path": wt, "base_commit": tree.get("origin_ref"),
                "apply_scope": "mission" if cumulative else "run", "previous_run_applied": previous_run_applied}

    def change_totals(self, run_id: str) -> Optional[Dict[str, int]]:
        """How much a finished chain of milestones would change in the project: files, and lines added and removed.
        For the operator's information only. The per-step caps stay the enforced limit; a mission of several steps
        can add up to more, and this is where that becomes visible. None when nothing differs or it cannot be read."""
        try:
            tree = self.tree_manifest(run_id, cumulative=True)
        except Exception:
            return None
        if not tree.get("ok") or not tree.get("manifest"):
            return None
        proj_root = Path((self.sm.get_run(run_id) or {}).get("project_path") or "").resolve()
        added = removed = new_files = 0
        for item in tree["manifest"]:
            new_files += item["type"] == "A"
            more, less = self._line_delta(proj_root / item["path"], Path(item["source"]))
            added, removed = added + more, removed + less
        return {"files": len(tree["manifest"]), "new_files": new_files, "added": added, "removed": removed}

    @staticmethod
    def _line_delta(old: Path, new: Path) -> Tuple[int, int]:
        """(lines added, lines removed) going from old to new. A binary or unreadable file counts as (0, 0): it is
        still counted as a file."""
        try:
            if not old.is_file():
                data = new.read_bytes()
                return (0, 0) if b"\0" in data else (len(data.decode("utf-8", errors="replace").splitlines()), 0)
            r = subprocess.run(["git", "-c", "core.quotepath=false", "diff", "--no-index", "--numstat", "--",
                                str(old), str(new)], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30)
            fields = (r.stdout.splitlines() or [""])[0].split("\t")
            return (int(fields[0]), int(fields[1])) if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit() else (0, 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0, 0

    def apply_tree(self, run_id: str, request_id: str = "", applied_by: str = "HUMAN", on_conflict: str = "block", *, cumulative: bool = True) -> Dict[str, Any]:
        """Apply a mission chain or one run to the project: backup, copy, re-verify in the project, roll back on
        any failure. on_conflict: 'block' refuses when the user changed a file the AIs also changed; 'skip' leaves
        those files alone and applies the rest. Deletions are never applied."""
        return self._apply_request(run_id, request_id, applied_by, cumulative=cumulative, on_conflict=on_conflict)

    # ------------------------------------------------------------------ discard (human action)
    def discard(self, run_id: str, request_id: str = "", by: str = "HUMAN") -> Dict[str, Any]:
        run = self.sm.get_run(run_id)
        if not run or run["current_state"] != State.PASS:
            return {"status": "refused", "reason": "only PASS results can be discarded"}
        st = self.result_state(run_id).get("state")
        if st in (RESULT_APPLIED, RESULT_DISCARDED):
            return {"status": "refused", "reason": f"result already {st}"}
        proj, wt = run.get("project_path") or "", run.get("worktree_path") or ""
        if not wt or not Path(wt).exists():
            self._set_state(run_id, RESULT_DISCARDED, "worktree already absent", request_id)
            self.sm.log_audit(run_id, by, "RESULT_DISCARDED", json.dumps({"worktree": wt, "removed": False, "request_id": request_id}))
            return {"status": "discarded", "run_id": run_id, "removed": False}
        if not self.worktree_contained(proj, wt) or Path(wt).resolve().name != run_id:
            return {"status": "refused", "reason": "worktree is not this run's directory inside the project's .director_worktrees"}
        # preserve evidence before removal
        try:
            changes = self.change_set(wt)
            self._save(run_id, "discard_evidence.json", {"run_id": run_id, "changed_files": changes, "file_diffs": self.file_diffs(wt, changes),
                                                         "at": datetime.utcnow().isoformat(), "worktree": wt})
        except Exception as e:
            self._save(run_id, "discard_evidence.json", {"run_id": run_id, "error": str(e)[:300]})
        rc, out, err = _git(["worktree", "remove", "--force", str(Path(wt).resolve())], proj)
        removed = rc == 0 and not Path(wt).exists()
        if not removed and Path(wt).exists():
            return {"status": "failed", "reason": f"git worktree remove failed: {err.strip()[:300]}"}
        self._set_state(run_id, RESULT_DISCARDED, "worktree removed; evidence preserved", request_id)
        try:
            self.sm.upsert_result_state(run_id, worktree_state="MISSING")
        except Exception:
            pass
        self.sm.log_audit(run_id, by, "RESULT_DISCARDED", json.dumps({"worktree": wt, "removed": True, "request_id": request_id}))
        return {"status": "discarded", "run_id": run_id, "removed": True}
