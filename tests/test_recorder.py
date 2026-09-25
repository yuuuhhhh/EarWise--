"""Disk writer integration tests; all output lives in temporary directories."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from app.recorder import Recorder, recover_sessions, atomic_json, EEG_FIELDS, LABEL_FIELDS


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mema-recorder-test-")
        self.root = Path(self.temporary.name)
        self.directory = self.root / "sub_001" / "day_01" / "round_01" / "session-test"
        self.failures = []
        self.failed = threading.Event()
        self.snapshot = dict(schema_version="1.0", session_id="session-test", subject_id="001",
                             session_status="recording", started_monotonic_ns=1000,
                             trials=[dict(trial_id="t1", condition="attention", completed=True, rating_submitted=True),
                                     dict(trial_id="t2", condition="relax", completed=True, rating_submitted=True)],
                             summary={}, termination_reason="")
        self.recorder = None

    def tearDown(self):
        if self.recorder and not self.recorder.closed:
            self.recorder.finish(self.terminal("aborted"), False)
        self.temporary.cleanup()

    def on_error(self, reason):
        self.failures.append(reason)
        self.failed.set()

    def initialize(self, **kwargs):
        self.recorder = Recorder(self.directory, copy.deepcopy(self.snapshot), self.on_error, **kwargs)
        self.recorder.initialize()
        return self.recorder

    def terminal(self, status="completed"):
        return {**copy.deepcopy(self.snapshot), "session_status": status, "termination_reason": ""}

    def eeg(self, index=0, **extra):
        return dict(session_id="session-test", sample_row_index=index, device_seq=index % 256,
                    stream_epoch_id=1, channel_0_raw=-8388608, channel_1_raw=8388607,
                    channel_0_uv=None, channel_1_uv=None, **extra)

    def event(self, kind, trial=None, **extra):
        value = dict(session_id="session-test", event_id=f"e-{time.monotonic_ns()}", event_type=kind,
                     trial_id=trial, details_json=json.dumps({"message": '保留中文，逗号,"引号"\n换行'}, ensure_ascii=False))
        value.update(extra)
        return value

    def complete_rows(self, *, invalid_trial_end=False, invalid_score=False):
        self.recorder.submit("eeg", self.eeg())
        for kind in ["SESSION_START", "BASELINE_START", "BASELINE_END"]:
            self.recorder.submit("event", self.event(kind))
        for index, condition in [(1, "attention"), (2, "relax")]:
            trial = f"t{index}"
            self.recorder.submit("event", self.event("TRIAL_START", trial))
            self.recorder.submit("event", self.event("TRIAL_END", "t1" if invalid_trial_end else trial))
            self.recorder.submit("event", self.event("RATING_SUBMITTED", trial, condition=condition,
                                                     **{f"{condition}_score": 0 if invalid_score else 3,
                                                        "confidence_score": 4}))
        self.recorder.submit("event", self.event("SESSION_END"))

    def read_rows(self, name):
        with (self.directory / name).open(encoding="utf-8", newline="") as source:
            return list(csv.DictReader(source))

    def test_complete_result_exact_three_files_utf8_csv_and_raw_counts(self):
        self.initialize()
        self.complete_rows()
        result = self.recorder.finish(self.terminal(), True)
        self.assertEqual(result["session_status"], "completed")
        self.assertEqual({p.name for p in self.directory.iterdir()}, {"eeg_raw.csv", "labels.csv", "config.json"})
        rows = self.read_rows("eeg_raw.csv")
        self.assertEqual(rows[0]["channel_0_raw"], "-8388608")
        self.assertEqual(rows[0]["channel_1_raw"], "8388607")
        self.assertEqual(rows[0]["channel_0_uv"], "")
        labels = self.read_rows("labels.csv")
        self.assertEqual(json.loads(labels[0]["details_json"])["message"], '保留中文，逗号,"引号"\n换行')
        self.assertEqual(result["summary"]["actual_rows"], 1)
        self.assertEqual(result["summary"]["event_rows"], 10)
        self.assertEqual(self.failures, [])

    def test_barrier_persists_questionnaire_before_return(self):
        self.initialize()
        self.recorder.submit("event", self.event("RATING_SUBMITTED", "t1", attention_score=2, confidence_score=5))
        self.recorder.barrier()
        rows = self.read_rows("labels.csv")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attention_score"], "2")
        self.assertEqual(rows[0]["confidence_score"], "5")

    def test_completion_rejects_missing_events_and_wrong_row_identity(self):
        self.initialize()
        self.recorder.submit("eeg", self.eeg(index=3))
        self.recorder.submit("event", self.event("SESSION_END"))
        result = self.recorder.finish(self.terminal(), True)
        self.assertEqual(result["session_status"], "save_failed")
        self.assertIn("行号", result["termination_reason"])

    def test_completion_rejects_wrong_trial_associations(self):
        self.initialize()
        self.complete_rows(invalid_trial_end=True)
        result = self.recorder.finish(self.terminal(), True)
        self.assertEqual(result["session_status"], "save_failed")

    def test_completion_rejects_persisted_invalid_scores(self):
        self.initialize()
        self.complete_rows(invalid_score=True)
        result = self.recorder.finish(self.terminal(), True)
        self.assertEqual(result["session_status"], "save_failed")

    def test_queue_overflow_is_visible_and_never_silently_discards(self):
        recorder = Recorder(self.directory, copy.deepcopy(self.snapshot), self.on_error, max_items=1)
        recorder.submit("eeg", self.eeg())
        with self.assertRaises(OSError):
            recorder.submit("eeg", self.eeg(1))
        self.assertEqual(recorder.queue.qsize(), 1)
        self.assertTrue(self.failures)
        self.assertIn("队列已满", recorder.error)

    def test_aged_queue_marks_save_failed_even_if_bytes_eventually_write(self):
        self.initialize(max_age_seconds=.1)
        self.recorder.queue.put((time.monotonic() - 10, "eeg", self.eeg()))
        self.assertTrue(self.failed.wait(2))
        result = self.recorder.finish(self.terminal(), False)
        self.assertEqual(result["session_status"], "save_failed")
        self.assertIn("持续积压", result["termination_reason"])

    def test_disk_exception_is_visible_and_does_not_claim_completion(self):
        self.initialize()
        self.recorder._ew.writerow = Mock(side_effect=OSError("simulated full disk"))
        self.recorder.submit("eeg", self.eeg())
        self.assertTrue(self.failed.wait(2))
        result = self.recorder.finish(self.terminal(), True)
        self.assertEqual(result["session_status"], "save_failed")
        self.assertIn("full disk", result["termination_reason"])
        self.assertEqual(result["summary"]["actual_rows"], 0)
        self.assertEqual(self.read_rows("eeg_raw.csv"), [])

    def test_existing_directory_is_never_overwritten(self):
        self.directory.mkdir(parents=True)
        marker = self.directory / "config.json"
        marker.write_text('{"preserve":true}', encoding="utf-8")
        recorder = Recorder(self.directory, self.snapshot, self.on_error)
        with self.assertRaises(FileExistsError):
            recorder.initialize()
        self.assertEqual(marker.read_text(encoding="utf-8"), '{"preserve":true}')

    def test_orphan_recovery_only_changes_recording_and_retains_raw_bytes(self):
        self.initialize()
        self.recorder.submit("eeg", self.eeg())
        self.recorder.submit("event", self.event("SESSION_START"))
        self.recorder.finish(self.terminal("recording"), False)
        raw_before = (self.directory / "eeg_raw.csv").read_bytes()
        terminal_paths = []
        for status in ["completed", "aborted", "interrupted", "save_failed"]:
            path = self.directory.parent / f"existing-{status}" / "config.json"
            path.parent.mkdir()
            atomic_json(path, dict(session_status=status, termination_reason="keep original reason"))
            terminal_paths.append((path, path.read_bytes()))
        recovered = recover_sessions(self.root)
        self.assertEqual(recovered, [str(self.directory)])
        config = json.loads((self.directory / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["session_status"], "interrupted")
        self.assertEqual((self.directory / "eeg_raw.csv").read_bytes(), raw_before)
        for path, data in terminal_paths:
            self.assertEqual(path.read_bytes(), data)
        recovery_event = self.read_rows("labels.csv")[-1]
        self.assertEqual(recovery_event["event_type"], "SESSION_INTERRUPTED")
        self.assertEqual(recovery_event["event_monotonic_ns"], "")
        self.assertTrue(json.loads(recovery_event["details_json"])["recovered_on_startup"])
        self.assertEqual(recover_sessions(self.root), [])


if __name__ == "__main__":
    unittest.main()
