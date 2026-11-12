"""
Canonical DB path resolver for AI DEV DIRECTOR.

Every component (StateMachine, ChatManager, ProviderManager, dashboard) must obtain the
SQLite path through get_db_path(). The path is absolute and anchored at the project source
root, so running the dashboard or CLI from a different cwd can never create a split-brain DB.

Test isolation: when DIRECTOR_TESTING=1 and TEST_DB_PATH is set, that path is returned instead.
"""
import os
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = SOURCE_ROOT / "data" / "director.db"


def get_db_path() -> Path:
    if os.environ.get("DIRECTOR_TESTING") == "1" and os.environ.get("TEST_DB_PATH"):
        return Path(os.environ["TEST_DB_PATH"]).resolve()
    return DEFAULT_DB_PATH


def connect(path=None, timeout: float = 30.0):
    """Every Director connection to the SQLite DB. Missions, the console and the bridge write from several threads
    at once; with the default rollback journal and no wait, a busy DB once failed whole mission steps ("database
    disk image is malformed" twice on 2026-11-10). WAL lets readers and one writer work together, and a writer waits
    up to `timeout` seconds for another instead of failing at once."""
    import sqlite3
    conn = sqlite3.connect(str(path or get_db_path()), timeout=timeout)
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    try:
        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            conn.execute("PRAGMA journal_mode = WAL")      # persistent in the DB file; a no-op once set
    except sqlite3.DatabaseError:
        pass                                               # another process is switching it right now: keep going
    return conn


def get_runs_dir() -> Path:
    """Where run evidence is kept. Under DIRECTOR_TESTING=1 the suite's TEST_RUNS_DIR keeps test runs out of the
    source tree (the same isolation rule as get_db_path): one full suite used to leave a few hundred RUN-* folders
    in <repo>/runs. (From digilogd-cmd/ai-dev-director PR #1 by edge0303.)"""
    if os.environ.get("DIRECTOR_TESTING") == "1" and os.environ.get("TEST_RUNS_DIR"):
        return Path(os.environ["TEST_RUNS_DIR"]).resolve()
    return SOURCE_ROOT / "runs"


def get_lock_dir() -> Path:
    """Directory for cross-process lock files (next to the DB; isolated under TEST_DB_PATH in tests)."""
    return get_db_path().parent / "locks"
