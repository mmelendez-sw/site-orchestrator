from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from enrichment import metrics_store


class MetricsRetryTests(unittest.TestCase):
    def setUp(self):
        metrics_store._ddl_done = False
        self.addCleanup(setattr, metrics_store, "_ddl_done", False)

    def _conn(self):
        conn = MagicMock()
        conn.cursor.return_value = MagicMock()
        return conn

    def test_deadlock_is_retried(self):
        calls = []

        def action(cursor):
            calls.append(1)
            if len(calls) == 1:
                raise Exception("[40001] Transaction was deadlocked ... (1205)")
            return "ok"

        with patch("enrichment.mssql.connect_mssql", side_effect=lambda: self._conn()), \
                patch.object(metrics_store, "ddl_statements", return_value=["SELECT 1"]), \
                patch("time.sleep"):
            self.assertEqual(metrics_store._with_connection(action), "ok")
        self.assertEqual(len(calls), 2)

    def test_other_errors_raise_and_ddl_runs_once(self):
        conns = [self._conn(), self._conn()]
        with patch("enrichment.mssql.connect_mssql", side_effect=conns), \
                patch.object(metrics_store, "ddl_statements", return_value=["SELECT 1"]):
            metrics_store._with_connection(lambda c: None)
            metrics_store._with_connection(lambda c: None)
            with self.assertRaises(ValueError):
                with patch("enrichment.mssql.connect_mssql", return_value=self._conn()):
                    metrics_store._with_connection(lambda c: (_ for _ in ()).throw(ValueError("bad row")))
        ddl_runs = [c for c in conns[1].cursor.return_value.execute.call_args_list if "applock" in str(c)]
        self.assertEqual(ddl_runs, [])  # second connection skipped the DDL


if __name__ == "__main__":
    unittest.main()
