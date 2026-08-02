import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlmodel import Session, SQLModel

from api import tasks
from core import db
from core.db import TaskRunModel


class TaskSnapshotPersistenceTests(unittest.TestCase):
    def test_small_log_list_is_preserved_without_marker(self):
        logs = ["first", "second"]

        self.assertEqual(tasks._bounded_persisted_task_logs(logs), logs)

    def test_one_oversized_log_line_is_truncated_to_the_byte_limit(self):
        persisted = tasks._bounded_persisted_task_logs(["大" * 300_000])

        encoded = json.dumps(persisted, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(encoded), tasks.MAX_PERSISTED_TASK_LOG_BYTES)
        self.assertIn("单条日志已截断", persisted[-1])

    def test_bounded_log_selection_uses_a_bounded_number_of_serializations(self):
        logs = [f"line-{index}: " + ("x" * 2_000) for index in range(1_000)]
        original_dumps = tasks._json_dumps

        with mock.patch.object(
            tasks,
            "_json_dumps",
            wraps=original_dumps,
        ) as dumps:
            persisted = tasks._bounded_persisted_task_logs(logs)

        self.assertEqual(persisted[-1], logs[-1])
        self.assertLessEqual(dumps.call_count, 15)

    def test_upsert_persists_only_a_bounded_recent_log_tail(self):
        test_engine = db._create_database_engine("sqlite://")
        SQLModel.metadata.create_all(test_engine)
        logs = [f"line-{index}: " + ("查询日志" * 300) for index in range(1_000)]
        snapshot = {
            "id": "task-large-log",
            "platform": "chatgpt",
            "status": "running",
            "total": 52,
            "progress": "47/52",
            "logs": logs,
        }

        try:
            with mock.patch.object(tasks, "engine", test_engine):
                tasks._upsert_task_run(snapshot)

            with Session(test_engine) as session:
                row = session.get(TaskRunModel, "task-large-log")
                self.assertIsNotNone(row)
                persisted_logs = json.loads(row.logs_json)

            self.assertLessEqual(
                len(row.logs_json.encode("utf-8")),
                tasks.MAX_PERSISTED_TASK_LOG_BYTES,
            )
            self.assertLessEqual(
                len(persisted_logs),
                tasks.MAX_PERSISTED_TASK_LOG_ENTRIES + 1,
            )
            self.assertIn("较早日志已省略", persisted_logs[0])
            self.assertEqual(persisted_logs[-1], logs[-1])
            self.assertNotIn(logs[0], persisted_logs)
        finally:
            test_engine.dispose()

    def test_snapshot_throttle_coalesces_repeated_log_writes(self):
        task_id = "task-throttle"
        tasks._task_snapshot_last_persisted_at.pop(task_id, None)
        self.addCleanup(tasks._task_snapshot_last_persisted_at.pop, task_id, None)

        with (
            mock.patch.object(
                tasks._task_store,
                "snapshot_if_present",
                return_value={"id": task_id, "status": "running"},
            ),
            mock.patch.object(
                tasks.time,
                "monotonic",
                side_effect=[100.0, 100.2, 101.1],
            ),
            mock.patch.object(
                tasks,
                "_persist_task_snapshot_best_effort",
                return_value=True,
            ) as persist,
        ):
            self.assertTrue(tasks._persist_task_snapshot_throttled(task_id))
            self.assertTrue(tasks._persist_task_snapshot_throttled(task_id))
            self.assertTrue(tasks._persist_task_snapshot_throttled(task_id))

        self.assertEqual(persist.call_count, 2)

    def test_failed_snapshot_write_uses_bounded_retry_cooldown(self):
        task_id = "task-throttle-retry"
        tasks._task_snapshot_last_persisted_at.pop(task_id, None)
        self.addCleanup(tasks._task_snapshot_last_persisted_at.pop, task_id, None)

        with (
            mock.patch.object(
                tasks._task_store,
                "snapshot_if_present",
                return_value={"id": task_id, "status": "running"},
            ),
            mock.patch.object(
                tasks.time,
                "monotonic",
                side_effect=[100.0, 100.1, 101.1],
            ),
            mock.patch.object(
                tasks,
                "_persist_task_snapshot_best_effort",
                side_effect=[False, True],
            ) as persist,
        ):
            self.assertFalse(tasks._persist_task_snapshot_throttled(task_id))
            self.assertTrue(tasks._persist_task_snapshot_throttled(task_id))
            self.assertTrue(tasks._persist_task_snapshot_throttled(task_id))

        self.assertEqual(persist.call_count, 2)

    def test_missing_task_skips_clock_and_database_write(self):
        with (
            mock.patch.object(tasks._task_store, "exists", return_value=False),
            mock.patch.object(tasks.time, "monotonic") as clock,
            mock.patch.object(tasks, "_persist_task_snapshot_best_effort") as persist,
        ):
            self.assertTrue(tasks._persist_task_snapshot_throttled("task-missing"))

        clock.assert_not_called()
        persist.assert_not_called()

    def test_terminal_snapshot_clears_throttle_state(self):
        task_id = "task-terminal"
        tasks._task_snapshot_last_persisted_at[task_id] = 100.0
        snapshot = {
            "id": task_id,
            "platform": "chatgpt",
            "status": "done",
            "logs": [],
        }

        with (
            mock.patch.object(
                tasks._task_store,
                "snapshot_if_present",
                return_value=snapshot,
            ),
            mock.patch.object(tasks, "_upsert_task_run"),
        ):
            tasks._persist_task_snapshot(task_id)

        self.assertNotIn(task_id, tasks._task_snapshot_last_persisted_at)

    def test_active_snapshot_returns_full_memory_logs_without_forced_write(self):
        task_id = "task-active-memory"
        snapshot = {
            "id": task_id,
            "platform": "chatgpt",
            "status": "running",
            "logs": [f"line-{index}" for index in range(800)],
        }

        with (
            mock.patch.object(
                tasks._task_store,
                "snapshot_if_present",
                return_value=snapshot,
            ),
            mock.patch.object(tasks, "_persist_task_snapshot") as forced_persist,
            mock.patch.object(
                tasks,
                "_persist_task_snapshot_throttled",
                return_value=True,
            ) as throttled_persist,
            mock.patch.object(tasks, "_get_persisted_task") as persisted_read,
        ):
            result = tasks._get_task_snapshot(task_id)

        self.assertEqual(result["logs"], snapshot["logs"])
        forced_persist.assert_not_called()
        throttled_persist.assert_called_once_with(task_id)
        persisted_read.assert_not_called()

    def test_active_snapshot_falls_back_to_database_after_concurrent_cleanup(self):
        task_id = "task-cleanup-race"
        persisted = {
            "id": task_id,
            "platform": "chatgpt",
            "status": "done",
            "logs": ["finished"],
        }

        with (
            mock.patch.object(tasks, "_ensure_task_exists"),
            mock.patch.object(tasks._task_store, "exists", return_value=True),
            mock.patch.object(
                tasks._task_store,
                "snapshot",
                side_effect=KeyError(task_id),
            ),
            mock.patch.object(
                tasks._task_store,
                "snapshot_if_present",
                return_value=None,
            ) as optional_snapshot,
            mock.patch.object(
                tasks,
                "_get_persisted_task",
                return_value=persisted,
            ),
        ):
            result = tasks._get_task_snapshot(task_id)

        optional_snapshot.assert_called_once_with(task_id)
        self.assertEqual(result, persisted)

    def test_sse_switches_to_database_when_cleanup_removes_memory_task(self):
        task_id = "task-sse-cleanup-race"
        running = {
            "id": task_id,
            "platform": "chatgpt",
            "status": "running",
            "logs": ["first"],
            "success": 0,
            "registered": 0,
            "total": 1,
        }
        terminal = {
            **running,
            "status": "done",
            "success": 1,
            "registered": 1,
        }

        async def run_stream():
            async def run_in_thread(function, *args, **kwargs):
                return function(*args, **kwargs)

            with (
                mock.patch.object(tasks, "_finalize_orphan_tasks"),
                mock.patch.object(tasks, "_ensure_task_exists"),
                mock.patch.object(tasks._task_store, "exists", return_value=True),
                mock.patch.object(
                    tasks._task_store,
                    "log_snapshot_if_present",
                    side_effect=[
                        (list(running["logs"]), "running", running),
                        None,
                    ],
                ),
                mock.patch.object(
                    tasks,
                    "_get_persisted_task",
                    return_value=terminal,
                ),
                mock.patch.object(
                    tasks,
                    "_persist_task_snapshot_throttled",
                    return_value=True,
                ),
                mock.patch.object(
                    tasks.asyncio,
                    "to_thread",
                    side_effect=run_in_thread,
                ) as to_thread,
                mock.patch.object(
                    tasks.asyncio,
                    "sleep",
                    new=mock.AsyncMock(),
                ),
            ):
                response = await tasks.stream_logs(task_id)
                iterator = response.body_iterator
                first_event = await anext(iterator)
                done_event = await anext(iterator)

            return first_event, done_event, to_thread

        first_event, done_event, to_thread = asyncio.run(run_stream())

        self.assertIn('"line": "first"', first_event)
        self.assertIn('"done": true', done_event)
        self.assertIn('"status": "done"', done_event)
        self.assertGreaterEqual(to_thread.call_count, 3)

    def test_snapshot_persistence_warning_does_not_include_exception_payload(self):
        secret = "TOKEN_SHOULD_NOT_REACH_LOGGER"

        with (
            mock.patch.object(
                tasks,
                "_persist_task_snapshot",
                side_effect=RuntimeError(secret),
            ),
            self.assertLogs(tasks.logger, level="WARNING") as captured,
        ):
            self.assertFalse(tasks._persist_task_snapshot_best_effort("task-secret"))

        rendered = "\n".join(captured.output)
        self.assertNotIn(secret, rendered)
        self.assertIn("RuntimeError", rendered)

    def test_log_uses_throttled_snapshot_persistence(self):
        with (
            mock.patch.object(tasks._task_store, "append_log") as append_log,
            mock.patch.object(
                tasks,
                "_persist_task_snapshot_throttled",
                return_value=True,
            ) as persist,
            mock.patch("builtins.print"),
        ):
            tasks._log("task-log", "demo")

        append_log.assert_called_once()
        persist.assert_called_once_with("task-log")


class SQLiteDurabilityTests(unittest.TestCase):
    def test_created_sqlite_engine_enables_wal_full_sync_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "durability.db"
            test_engine = db._create_database_engine(f"sqlite:///{db_path}")
            try:
                raw = test_engine.raw_connection()
                try:
                    cursor = raw.cursor()
                    journal_mode = cursor.execute("PRAGMA journal_mode").fetchone()[0]
                    synchronous = cursor.execute("PRAGMA synchronous").fetchone()[0]
                    busy_timeout = cursor.execute("PRAGMA busy_timeout").fetchone()[0]
                    cursor.close()
                finally:
                    raw.close()
            finally:
                test_engine.dispose()

        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertEqual(int(synchronous), 2)
        self.assertGreaterEqual(int(busy_timeout), 30_000)

    def test_database_engine_hides_bound_parameters_from_errors(self):
        test_engine = db._create_database_engine("sqlite://")
        try:
            self.assertTrue(test_engine.hide_parameters)
        finally:
            test_engine.dispose()

    def test_connection_pragma_helper_supports_raw_sqlite_connections(self):
        connection = sqlite3.connect(":memory:")
        try:
            db._configure_sqlite_connection(connection, None)
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
            synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
        finally:
            connection.close()

        self.assertGreaterEqual(int(busy_timeout), 30_000)
        self.assertEqual(int(synchronous), 2)


if __name__ == "__main__":
    unittest.main()
