"""LLM_Last_Attempted_Date__c: every attempted site is stamped, misses stay queued."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from enrichment.constants import BUCKET_OTHER, BUCKET_POTENTIAL_UPDATE
from enrichment.pipeline import _collect_apply_rows, _status_counts
from enrichment.sf_ops import (
    apply_updates_batch,
    attempted_field,
    build_row_payload,
    status_from_entry,
)
from enrichment.tests.test_throughput import _BatchClient, _tower_row

FIELD = "LLM_Last_Attempted_Date__c"
WHEN = "2026-11-02T18:00:00Z"
ON = {"LLM_ATTEMPTED_FIELD": FIELD}


def _collect(rows, **overrides):
    kwargs = dict(
        dequeue_holdouts=True,
        db_only=False,
        confirm_rooftop=False,
        confirm_existing=False,
        connectx_audit=False,
    )
    kwargs.update(overrides)
    return _collect_apply_rows(rows, **kwargs)


def _hit(sf_id="a01"):
    return {**_tower_row(sf_id), "bucket": BUCKET_POTENTIAL_UPDATE, "attempted_at": WHEN}


def _miss(sf_id="a02", reason="db_only_no_unique_hit"):
    return {"Id": sf_id, "bucket": BUCKET_OTHER, "holdout_reason": reason, "attempted_at": WHEN}


@patch.dict(os.environ, ON)
class AttemptDateTests(unittest.TestCase):
    def test_field_toggle(self):
        self.assertEqual(attempted_field(), FIELD)
        for off in ("0", "off", "none"):
            with patch.dict(os.environ, {"LLM_ATTEMPTED_FIELD": off}):
                self.assertIsNone(attempted_field())

    def test_every_decision_payload_carries_the_attempt_date(self):
        self.assertEqual(build_row_payload(_hit())[FIELD], WHEN)
        holdout = build_row_payload({"Id": "a03", "naip_site_type": "other", "attempted_at": WHEN})
        self.assertEqual(holdout, {"LLM_Classified__c": False, "LLM_Holdout__c": True, FIELD: WHEN})

    def test_db_only_miss_is_stamped_not_classified(self):
        hit, miss = _hit(), _miss()
        rows = _collect([hit, miss], db_only=True, dequeue_holdouts=False)
        self.assertEqual([r["Id"] for r in rows], ["a01", "a02"])
        self.assertEqual(build_row_payload(miss, write_holdout=False), {FIELD: WHEN})
        hit_payload = build_row_payload(hit, write_holdout=False)
        self.assertIs(hit_payload["LLM_Classified__c"], True)
        self.assertEqual(hit_payload[FIELD], WHEN)

    def test_left_failures_are_stamped_without_queue_flags(self):
        inconclusive = _miss("a04", reason="audit_inconclusive")
        rows = _collect([inconclusive], connectx_audit=True)
        self.assertEqual(rows, [inconclusive])
        self.assertEqual(build_row_payload(inconclusive), {FIELD: WHEN})

    def test_transient_holdouts_are_not_stamped(self):
        rows = _collect([_miss("a05", "sql_error"), _miss("a06", "nearmap_budget")])
        self.assertEqual(rows, [])

    def test_field_off_restores_old_selection(self):
        with patch.dict(os.environ, {"LLM_ATTEMPTED_FIELD": "0"}):
            self.assertEqual(_collect([_miss()], db_only=True, dequeue_holdouts=False), [])
            self.assertNotIn(FIELD, build_row_payload(_hit()))

    def test_batch_apply_status_and_no_holdout_fallback(self):
        miss = _miss("a07")
        _collect([miss], connectx_audit=True)
        client = _BatchClient(fail_ids={"a08"})
        failing_hit = {**_hit("a08")}
        entries = apply_updates_batch(client, [miss, failing_hit], verbose=False)
        by_id = {e["Id"]: e for e in entries}
        self.assertEqual(status_from_entry(by_id["a07"]), "stamped")
        # A failed decision write still dequeues, and keeps its attempt date.
        self.assertEqual(
            by_id["a08"]["payload"],
            {"LLM_Classified__c": False, "LLM_Holdout__c": True, FIELD: WHEN},
        )
        rows = [{"sf_update_status": status_from_entry(e)} for e in entries]
        self.assertEqual(_status_counts(rows)["stamped"], 1)

    def test_queue_filter_not_attempted_since(self):
        from enrichment.sf_ops import build_blank_site_type_query, parse_not_attempted_since

        since = parse_not_attempted_since("2026-11-01")
        self.assertEqual(since, "2026-11-01T00:00:00Z")
        soql = build_blank_site_type_query(not_attempted_since=since)
        self.assertIn(f"({FIELD} = null OR {FIELD} < 2026-11-01T00:00:00Z)", soql)
        self.assertNotIn(FIELD, build_blank_site_type_query())
        self.assertIsNone(parse_not_attempted_since(""))
        with self.assertRaises(ValueError):
            parse_not_attempted_since("last week")

    def test_failed_attempt_only_write_is_not_turned_into_a_holdout(self):
        miss = _miss("a09")
        _collect([miss], connectx_audit=True)
        client = _BatchClient()
        client.sf.restful = lambda *a, **k: [
            {"id": "a09", "success": False, "errors": [{"statusCode": "X", "message": "boom"}]}
        ]
        client.sf.Site__c.update = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        entries = apply_updates_batch(client, [miss, {**miss, "Id": "a10"}], verbose=False)
        self.assertTrue(all(e["status"] == "failed" for e in entries))
        self.assertTrue(all(e["payload"] == {FIELD: WHEN} for e in entries))


DUP = "Potential_Duplicate__c"
ALLOW = {"Sforce-Duplicate-Rule-Header": "allowSave=true"}
DUP_ERROR = {"statusCode": "DUPLICATES_DETECTED", "message": "Same site address exists in the system."}


class _DupSF:
    """Duplicate rule rejects every write to ``dup_ids`` unless allowSave is sent."""

    def __init__(self, dup_ids, other_error_ids=()):
        self.dup_ids, self.other_error_ids = set(dup_ids), set(other_error_ids)
        self.writes: list[tuple[str, dict, dict | None]] = []
        self.Site__c = self

    def _one(self, sf_id, payload, headers):
        self.writes.append((sf_id, dict(payload), headers))
        if sf_id in self.other_error_ids and headers is None:
            return {"statusCode": "FIELD_CUSTOM_VALIDATION_EXCEPTION", "message": "nope"}
        if sf_id in self.dup_ids and headers != ALLOW:
            return DUP_ERROR
        return None

    def update(self, sf_id, payload, headers=None):
        error = self._one(sf_id, payload, headers)
        if error:
            raise RuntimeError(f"{error['statusCode']}: {error['message']}")
        return 204

    def restful(self, path, method="GET", json=None, headers=None, **_kwargs):
        out = []
        for rec in json["records"]:
            payload = {k: v for k, v in rec.items() if k not in {"attributes", "id"}}
            error = self._one(rec["id"], payload, headers)
            out.append({"id": rec["id"], "success": error is None, "errors": [error] if error else []})
        return out


class _DupClient:
    def __init__(self, *args, **kwargs):
        self.sf = _DupSF(*args, **kwargs)


@patch.dict(os.environ, {**ON, "DUPLICATE_FLAG_FIELD": DUP})
class DuplicateFlagTests(unittest.TestCase):
    def _retries(self, client, sf_id):
        return [(p, h) for i, p, h in client.sf.writes if i == sf_id][1:]

    def test_duplicate_hit_dequeues_and_flags_through_the_rule(self):
        client = _DupClient(dup_ids={"a01"})
        entries = apply_updates_batch(client, [_hit("a01"), _hit("a02")], verbose=False)
        by_id = {e["Id"]: e for e in entries}
        self.assertEqual(status_from_entry(by_id["a02"]), "updated")
        self.assertEqual(
            self._retries(client, "a01"),
            [({"LLM_Classified__c": False, "LLM_Holdout__c": True, DUP: True, FIELD: WHEN}, ALLOW)],
        )
        self.assertEqual(status_from_entry(by_id["a01"]), "dequeued")
        # The decision write itself never bypasses the duplicate rule.
        self.assertTrue(all(h is None for i, _p, h in client.sf.writes[:2]))

    def test_leave_failure_modes_only_tick_the_flag(self):
        client = _DupClient(dup_ids={"a03"})
        entries = apply_updates_batch(
            client, [_hit("a03")], verbose=False, error_holdout=False
        )
        self.assertEqual(self._retries(client, "a03"), [({DUP: True, FIELD: WHEN}, ALLOW)])
        self.assertEqual(status_from_entry(entries[0]), "duplicate_flagged")
        self.assertEqual(_status_counts([{"sf_update_status": "duplicate_flagged"}])["duplicate_flagged"], 1)

    def test_attempt_only_duplicate_is_flagged(self):
        miss = _miss("a04")
        _collect([miss], connectx_audit=True)
        client = _DupClient(dup_ids={"a04"})
        entries = apply_updates_batch(client, [miss], verbose=False, error_holdout=False)
        self.assertEqual(entries[0]["payload"], {DUP: True, FIELD: WHEN})
        self.assertEqual(status_from_entry(entries[0]), "duplicate_flagged")

    def test_other_errors_are_not_flagged(self):
        client = _DupClient(dup_ids=(), other_error_ids={"a05"})
        entries = apply_updates_batch(client, [_hit("a05")], verbose=False, error_holdout=False)
        self.assertEqual(entries[0]["status"], "failed")
        self.assertEqual(len(client.sf.writes), 1)

    def test_single_row_path_flags_too(self):
        from enrichment.sf_ops import apply_one_update

        client = _DupClient(dup_ids={"a06"})
        entry = apply_one_update(client, _hit("a06"), verbose=False, error_holdout=False)
        self.assertEqual(entry["status"], "duplicate_flagged")
        self.assertEqual(self._retries(client, "a06"), [({DUP: True, FIELD: WHEN}, ALLOW)])

    def test_flag_off_keeps_old_behavior(self):
        with patch.dict(os.environ, {"DUPLICATE_FLAG_FIELD": "0"}):
            client = _DupClient(dup_ids={"a07"})
            entries = apply_updates_batch(client, [_hit("a07")], verbose=False)
            self.assertEqual(entries[0]["status"], "failed")
            self.assertEqual(
                self._retries(client, "a07"),
                [({"LLM_Classified__c": False, "LLM_Holdout__c": True, FIELD: WHEN}, None)],
            )


if __name__ == "__main__":
    unittest.main()
