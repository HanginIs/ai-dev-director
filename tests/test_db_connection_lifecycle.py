"""Connection cleanup with real private DBs; no production DB, CLI or HTTP calls."""
import gc
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

from core import db_path


def assert_closed(conn):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_context_commits_then_closes(tmp_path):
    path = tmp_path / "commit.db"
    with db_path.connect(path) as conn:
        conn.execute("CREATE TABLE sample(value INTEGER)")
        conn.execute("INSERT INTO sample VALUES (7)")
    assert_closed(conn)
    with db_path.connect(path) as reader:
        assert reader.execute("SELECT value FROM sample").fetchone() == (7,)
    assert_closed(reader)


def test_context_rolls_back_then_closes_without_swallowing_error(tmp_path):
    path = tmp_path / "rollback.db"
    with db_path.connect(path) as conn:
        conn.execute("CREATE TABLE sample(value INTEGER)")
        conn.execute("INSERT INTO sample VALUES (7)")
    with pytest.raises(RuntimeError, match="original failure"):
        with db_path.connect(path) as failed:
            failed.execute("INSERT INTO sample VALUES (8)")
            raise RuntimeError("original failure")
    assert_closed(failed)
    with db_path.connect(path) as reader:
        assert reader.execute("SELECT value FROM sample").fetchall() == [(7,)]


def test_failed_commit_also_closes_and_rolls_back(tmp_path):
    path = tmp_path / "deferred.db"
    with db_path.connect(path) as conn:
        conn.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE child(parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)")
    with pytest.raises(sqlite3.IntegrityError):
        with db_path.connect(path) as failed:
            failed.execute("PRAGMA foreign_keys=ON")
            failed.execute("INSERT INTO child VALUES (123)")
    assert_closed(failed)
    with db_path.connect(path) as reader:
        assert reader.execute("SELECT COUNT(*) FROM child").fetchone() == (0,)


def test_wal_and_default_thirty_second_wait_are_preserved(tmp_path):
    with db_path.connect(tmp_path / "defaults.db") as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert conn.execute("PRAGMA busy_timeout").fetchone() == (30000,)
        assert conn.row_factory is None


def test_custom_wait_and_caller_row_factory_are_preserved(tmp_path):
    with db_path.connect(tmp_path / "custom.db", timeout=1.25) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1250
        assert conn.execute("SELECT 7 AS value").fetchone()["value"] == 7
    assert_closed(conn)


def test_plain_connection_remains_usable_until_explicitly_closed(tmp_path):
    conn = db_path.connect(tmp_path / "explicit.db")
    try:
        assert conn.execute("SELECT 1").fetchone() == (1,)
        conn.commit()
        assert conn.execute("SELECT 2").fetchone() == (2,)
    finally:
        conn.close()
    assert_closed(conn)


def test_default_path_remains_test_isolated(isolate_provider_db):
    with db_path.connect() as conn:
        database = conn.execute("PRAGMA database_list").fetchone()[2]
        assert database == str(isolate_provider_db)
    assert_closed(conn)


def test_setup_failure_closes_before_reraising():
    conn = mock.Mock()
    conn.execute.side_effect = sqlite3.OperationalError("setup failed")
    with mock.patch.object(db_path.sqlite3, "connect", return_value=conn):
        with pytest.raises(sqlite3.OperationalError, match="setup failed"):
            db_path.connect("unused.db")
    conn.close.assert_called_once_with()


def test_journal_query_error_keeps_existing_tolerance():
    conn = mock.Mock()
    conn.execute.side_effect = [None, sqlite3.DatabaseError("journal mode busy")]
    with mock.patch.object(db_path.sqlite3, "connect", return_value=conn):
        assert db_path.connect("unused.db") is conn
    conn.close.assert_not_called()


@pytest.mark.skipif(not os.path.isdir("/dev/fd"), reason="requires file-descriptor enumeration")
def test_repeated_component_requests_do_not_wait_for_garbage_collection():
    from core.chat_manager import ChatManager
    from core.provider_manager import ProviderManager
    from core.state_machine import StateMachine
    from core import usage
    import dashboard_server

    state, chat, providers = StateMachine(), ChatManager(), ProviderManager()
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        before = len(os.listdir("/dev/fd"))
        for _ in range(40):
            state._init_db()
            chat._init_db()
            providers._sync_from_db()
            with usage._conn() as conn:
                conn.execute("SELECT 1").fetchone()
            with dashboard_server.get_db_conn() as conn:
                conn.execute("SELECT 1").fetchone()
        assert len(os.listdir("/dev/fd")) - before <= 2
    finally:
        if was_enabled:
            gc.enable()


def test_parallel_readers_and_writers_keep_wal_and_data(tmp_path):
    path = tmp_path / "parallel.db"
    with db_path.connect(path) as conn:
        conn.execute("CREATE TABLE sample(value INTEGER PRIMARY KEY)")

    def write(value):
        with db_path.connect(path) as conn:
            conn.execute("INSERT INTO sample VALUES (?)", (value,))
        assert_closed(conn)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(40)))
    with db_path.connect(path) as reader:
        assert reader.execute("SELECT COUNT(*) FROM sample").fetchone() == (40,)
        assert reader.execute("PRAGMA journal_mode").fetchone() == ("wal",)
