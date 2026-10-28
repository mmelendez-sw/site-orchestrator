from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from enrichment import mssql


class WakeRetryTests(unittest.TestCase):
    def test_retries_only_while_database_resumes(self):
        waking = Exception("[SQL Server]Database 'x' is not currently available. (40613)")
        calls = []

        def once(_cs=None):
            calls.append(1)
            if len(calls) < 3:
                raise waking
            return "conn"

        with patch.dict(os.environ, {"AZURE_SQL_WAKE_WAIT_S": "0"}), \
                patch.object(mssql, "_connect_mssql_once", side_effect=once):
            self.assertEqual(mssql.connect_mssql(), "conn")
        self.assertEqual(len(calls), 3)

    def test_other_errors_raise_immediately(self):
        with patch.dict(os.environ, {"AZURE_SQL_WAKE_WAIT_S": "0"}), \
                patch.object(mssql, "_connect_mssql_once", side_effect=ValueError("login failed")) as once:
            with self.assertRaises(ValueError):
                mssql.connect_mssql()
        self.assertEqual(once.call_count, 1)


if __name__ == "__main__":
    unittest.main()
