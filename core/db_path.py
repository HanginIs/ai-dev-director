"""
Canonical DB path resolver for AI DEV DIRECTOR.

Every component (StateMachine, ChatManager, ProviderManager, dashboard) must obtain the
SQLite path through get_db_path(). The path is absolute and anchored at the project source
root, so running the dashboard or CLI from a different cwd can never create a split-brain DB.

Test isolation: when DIRECTOR_TESTING=1 and TEST_DB_PATH is set, that path is returned instead.
"""
import os
import sqlite3
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = SOURCE_ROOT / "data" / "director.db"


def get_db_path() -> Path:
    if os.environ.get("DIRECTOR_TESTING") == "1" and os.environ.get("TEST_DB_PATH"):
        return Path(os.environ["TEST_DB_PATH"]).resolve()
    return DEFAULT_DB_PATH


class ClosingConnection(sqlite3.Connection):
    """Finish the transaction, then release its resources even if commit fails.

    sqlite3's own context manager commits/rolls back but does not close. Keep
    its transaction semantics; only add deterministic cleanup (inspired by
    eunsebi/ai-dev-director, cecace30795d3a180e0344fc463f215bc315b0cf).
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect(path=None, timeout: float = 30.0):
    """Every Director connection to the SQLite DB. Missions, the console and the bridge write from several threads
    at once; with the default rollback journal and no wait, a busy DB once failed whole mission steps ("database
    disk image is malformed" twice on 2026-11-10). WAL lets readers and one writer work together, and a writer waits
    up to `timeout` seconds for another instead of failing at once."""
    conn = sqlite3.connect(str(path or get_db_path()), timeout=timeout, factory=ClosingConnection)
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                conn.execute("PRAGMA journal_mode = WAL")  # persistent; a no-op once set
        except sqlite3.DatabaseError:
            pass                                           # another process may be switching journal mode
        return conn
    except BaseException:
        conn.close()                                       # failed setup must not leak a connection either
        raise


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
