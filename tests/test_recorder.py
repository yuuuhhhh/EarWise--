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

from app.configuration import EXPERIMENT_PROTOCOL_ID, plan_fingerprint, randomization_for
from app.recorder import Recorder, recover_sessions, atomic_json, EEG_FIELDS, LABEL_FIELDS


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mema-recorder-test-")
        self.root = Path(self.temporary.name)
        self.directory = self.root / "sub_001" / "round_01" / "session-test"
        self.failures = []
        self.failed = threading.Event()
        self.snapshot = self.planned_snapshot(round=1, first="attention")
        self.recorder = None

    def planned_snapshot(self, *, round, first):
        subject = next(f"recorder-{first}-{number}" for number in range(100)
                       if randomization_for(f"recorder-{first}-{number}", round)["first_condition"] == first)
        randomization = randomization_for(subject, round)
        plan = []
        conditions = (first, "relax" if first == "attention" else "attention")
        for order in range(1, 5):
            condition = conditions[(order - 1) % 2]
            video_index = (order - 1) // 2 + 1 + ((round - 1) * 2 if condition == "attention" else 0)
            path = f"{condition}_video/{video_index:02d}.mp4"
            plan.append(dict(condition=condition, trial_order=order,
                             video=dict(video_id=f"{condition}_{video_index:02d}", path=path,
                                        filename=f"{video_index:02d}.mp4", url=f"/media/{path}",
                                        duration_seconds=4.0, codecs=["avc1", "mp4a"], size_bytes=100,
                                        mtime_ns=100, ctime_ns=100, sha256="a" * 64)))
        return dict(schema_version="2.0", session_id="session-test", subject_id=subject,
                    experiment_protocol_id=EXPERIMENT_PROTOCOL_ID, round=round,
                    randomization=randomization, plan_id=plan_fingerprint(subject, round, plan),
                    session_status="recording", started_monotonic_ns=1000,
                    trials=[dict(item, trial_id=f"t{item['trial_order']}", completed=True,
                                 rating_submitted=True) for item in plan],
                    summary={}, termination_reason="")

    def refresh_plan_id(self, snapshot):
        plan = [{key: trial[key] for key in ("condition", "trial_order", "video")}
                for trial in snapshot["trials"]]
        snapshot["plan_id"] = plan_fingerprint(snapshot["subject_id"], snapshot["round"], plan)

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
        value = dict(session_id="session-test", subject_id=self.snapshot["subject_id"],
                    round=self.snapshot["round"], sample_row_index=index, device_seq=index % 256,
                    stream_epoch_id=1, channel_0_raw=-8388608, channel_1_raw=8388607,
                    channel_0_uv=None, channel_1_uv=None)
        value.update(extra)
        return value

    def event(self, kind, trial=None, **extra):
        value = dict(session_id="session-test", subject_id=self.snapshot["subject_id"], round=self.snapshot["round"],
                     event_id=f"e-{time.monotonic_ns()}", event_type=kind,
                     trial_id=trial, details_json=json.dumps({"message": '保留中文，逗号,"引号"\n换行'}, ensure_ascii=False))
        metadata = next((item for item in self.snapshot["trials"] if item["trial_id"] == trial), None)
        if metadata:
            value.update(trial_order=metadata["trial_order"], condition=metadata["condition"],
                         video_id=metadata["video"]["video_id"], video_order_in_trial=1)
        value.update(extra)
        return value

    def complete_rows(self, *, invalid_trial_end=False, invalid_score=False, trial_count=4, transform=None):
        self.recorder.submit("eeg", self.eeg())
        events = []
        for kind in ["SESSION_START", "BASELINE_START", "BASELINE_END"]:
            events.append(self.event(kind))
        for index, metadata in enumerate(self.snapshot["trials"][:trial_count], 1):
            trial, condition = metadata["trial_id"], metadata["condition"]
            events.append(self.event("TRIAL_START", trial))
            events.append(self.event("TRIAL_END", "t1" if invalid_trial_end else trial))
            events.append(self.event("RATING_SUBMITTED", trial, condition=condition,
                                     **{f"{condition}_score": 0 if invalid_score else index,
                                        "confidence_score": 5 - index}))
        events.append(self.event("SESSION_END"))
        if transform:
            events = transform(events)
        for event in events:
            self.recorder.submit("event", event)

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
        self.assertEqual(result["summary"]["event_rows"], 16)
        self.assertEqual(result["schema_version"], "2.0")
        self.assertNotIn("day", result)
        self.assertEqual(result["experiment_protocol_id"], EXPERIMENT_PROTOCOL_ID)
        ratings = {row["trial_id"]: row for row in labels if row["event_type"] == "RATING_SUBMITTED"}
        for index in range(1, 5):
            condition = "attention" if index % 2 else "relax"
            self.assertEqual(ratings[f"t{index}"][f"{condition}_score"], str(index))
            self.assertEqual(ratings[f"t{index}"]["confidence_score"], str(5 - index))
            self.assertEqual(ratings[f"t{index}"]["video_id"], f"{condition}_{(index - 1) // 2 + 1:02d}")
        persisted = json.loads((self.directory / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["randomization"], self.snapshot["randomization"])
        self.assertEqual(persisted["plan_id"], self.snapshot["plan_id"])
        self.assertEqual(self.failures, [])

    def test_both_random_first_conditions_complete_on_all_three_rounds(self):
        for rnd in (1, 2, 3):
            for first in ("attention", "relax"):
                with self.subTest(round=rnd, first=first):
                    self.directory = self.root / "sub_001" / f"round_{rnd:02d}" / first
                    self.snapshot = self.planned_snapshot(round=rnd, first=first)
                    self.initialize()
                    self.complete_rows()
                    result = self.recorder.finish(self.terminal(), True)
                    self.assertEqual(result["session_status"], "completed", result["termination_reason"])
                    for name in ("eeg_raw.csv", "labels.csv"):
                        rows = self.read_rows(name)
                        self.assertTrue(rows)
                        self.assertEqual(list(rows[0])[:3], ["session_id", "subject_id", "round"])
                        for row in rows:
                            self.assertEqual(row["subject_id"], self.snapshot["subject_id"])
                            self.assertEqual(row["round"], str(rnd))
                            self.assertNotIn("day", row)

    def test_only_two_finished_trials_cannot_complete_round(self):
        self.initialize()
        self.complete_rows(trial_count=2)
        result = self.recorder.finish(self.terminal(), True)
        self.assertEqual(result["session_status"], "save_failed")
        self.assertIn("完整性", result["termination_reason"])

    def test_two_trial_snapshot_cannot_bypass_four_trial_requirement(self):
        self.snapshot["trials"] = self.snapshot["trials"][:2]
        self.initialize()
        self.complete_rows(trial_count=2)
        result = self.recorder.finish(self.terminal(), True)
        self.assertEqual(result["session_status"], "save_failed")
        self.assertIn("trial 数量", result["termination_reason"])

    def test_missing_third_or_fourth_trial_boundaries_or_rating_fails(self):
        for trial_id in ("t3", "t4"):
            for kind in ("TRIAL_START", "TRIAL_END", "RATING_SUBMITTED"):
                with self.subTest(trial=trial_id, kind=kind):
                    self.directory = self.directory.with_name(f"missing-{trial_id}-{kind}")
                    self.initialize()
                    self.complete_rows(transform=lambda events: [row for row in events
                                       if (row["trial_id"], row["event_type"]) != (trial_id, kind)])
                    self.assertEqual(self.recorder.finish(self.terminal(), True)["session_status"], "save_failed")

    def test_snapshot_duplicate_identity_wrong_order_or_wrong_condition_fails(self):
        variants = (
            ("duplicate-trial", lambda trials: trials[2].update(trial_id="t1")),
            ("duplicate-video", lambda trials: trials[2]["video"].update(video_id="attention_01")),
            ("wrong-order", lambda trials: trials[2].update(trial_order=1)),
            ("wrong-condition", lambda trials: trials[2].update(condition="relax")),
        )
        original = copy.deepcopy(self.snapshot)
        for name, mutate in variants:
            with self.subTest(name=name):
                self.directory = self.directory.with_name(name)
                self.snapshot = copy.deepcopy(original)
                self.initialize()
                self.complete_rows()
                terminal = self.terminal()
                mutate(terminal["trials"])
                self.assertEqual(self.recorder.finish(terminal, True)["session_status"], "save_failed")

    def test_persisted_trial_metadata_cannot_point_to_other_same_condition_trial(self):
        for kind in ("TRIAL_START", "TRIAL_END", "RATING_SUBMITTED"):
            for field, value in (("trial_order", 1), ("video_id", "attention_01"), ("condition", "relax")):
                with self.subTest(kind=kind, field=field):
                    self.directory = self.directory.with_name(f"wrong-{kind}-{field}")
                    self.initialize()
                    def change(events):
                        for row in events:
                            if row["event_type"] == kind and row["trial_id"] == "t3":
                                row[field] = value
                        return events
                    self.complete_rows(transform=change)
                    result = self.recorder.finish(self.terminal(), True)
                    self.assertEqual(result["session_status"], "save_failed")
                    self.assertIn("关联", result["termination_reason"])

    def test_same_condition_trials_cannot_exchange_execution_order(self):
        self.initialize()
        def exchange(events):
            # The two attention trials keep internally consistent metadata, but exchange places.
            return events[:3] + events[9:12] + events[6:9] + events[3:6] + events[12:]
        self.complete_rows(transform=exchange)
        result = self.recorder.finish(self.terminal(), True)
        self.assertEqual(result["session_status"], "save_failed")
        self.assertIn("执行顺序", result["termination_reason"])

    def test_rating_cannot_be_persisted_before_trial_end(self):
        self.initialize()
        def reorder(events):
            events[10], events[11] = events[11], events[10]
            return events
        self.complete_rows(transform=reorder)
        result = self.recorder.finish(self.terminal(), True)
        self.assertEqual(result["session_status"], "save_failed")
        self.assertIn("提交顺序", result["termination_reason"])

    def test_unknown_protocol_cannot_be_marked_completed(self):
        for protocol in ("legacy-two-trial", "earwise-3day-2round-4trial-v2"):
            with self.subTest(protocol=protocol):
                self.directory = self.directory.with_name(protocol)
                self.initialize()
                self.complete_rows()
                terminal = self.terminal()
                terminal["experiment_protocol_id"] = protocol
                self.assertEqual(self.recorder.finish(terminal, True)["session_status"], "save_failed")

    def test_round_must_be_exact_integer_in_three_round_protocol(self):
        for rnd in (0, 4, True, 1.0, "1", None):
            with self.subTest(round=rnd):
                self.directory = self.directory.with_name(f"invalid-round-{rnd}")
                self.snapshot["round"] = rnd
                self.initialize()
                self.complete_rows()
                result = self.recorder.finish(self.terminal(), True)
                self.assertEqual(result["session_status"], "save_failed")
                self.assertIn("round 编号", result["termination_reason"])

    def test_new_snapshots_reject_legacy_day_or_wrong_data_schema(self):
        for field, value in (("day", 1), ("day", None), ("schema_version", "1.0"), ("schema_version", 2.0)):
            with self.subTest(field=field, value=value):
                self.directory = self.directory.with_name(f"invalid-{field}-{value}")
                self.initialize()
                self.complete_rows()
                terminal = self.terminal()
                terminal[field] = value
                result = self.recorder.finish(terminal, True)
                self.assertEqual(result["session_status"], "save_failed")
                self.assertIn("schema", result["termination_reason"])

    def test_randomization_metadata_must_match_subject_and_round(self):
        mutations = (
            ("missing", lambda snapshot: snapshot.pop("randomization")),
            ("method", lambda snapshot: snapshot["randomization"].update(method="untracked")),
            ("seed", lambda snapshot: snapshot["randomization"].update(seed="0" * 64)),
            ("first", lambda snapshot: snapshot["randomization"].update(first_condition="relax")),
            ("retry", lambda snapshot: snapshot["randomization"].update(retry_policy="reroll")),
            ("scope", lambda snapshot: snapshot["randomization"].update(scope="subject+day")),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                self.directory = self.directory.with_name(f"invalid-randomization-{name}")
                self.initialize()
                self.complete_rows()
                terminal = self.terminal()
                mutate(terminal)
                result = self.recorder.finish(terminal, True)
                self.assertEqual(result["session_status"], "save_failed")
                self.assertIn("随机顺序", result["termination_reason"])

    def test_every_eeg_and_label_row_must_keep_subject_and_round_even_when_aborted(self):
        for status in ("completed", "aborted"):
            for kind in ("eeg", "event"):
                for field, value in (("round", 2), ("round", ""), ("subject_id", "other-subject"),
                                     ("subject_id", ""), ("session_id", "other-session")):
                    with self.subTest(status=status, kind=kind, field=field, value=value):
                        self.directory = self.directory.with_name(f"identity-{status}-{kind}-{field}-{value}")
                        self.initialize()
                        if status == "completed":
                            self.complete_rows()
                        else:
                            self.recorder.submit("eeg", self.eeg())
                            self.recorder.submit("event", self.event("SESSION_START"))
                        invalid = self.eeg(1, **{field: value}) if kind == "eeg" else self.event("DIAGNOSTIC", **{field: value})
                        self.recorder.submit(kind, invalid)
                        result = self.recorder.finish(self.terminal(status), status == "completed")
                        self.assertEqual(result["session_status"], "save_failed")
                        self.assertIn("身份", result["termination_reason"])

    def test_snapshot_subject_or_round_cannot_relabel_persisted_recording(self):
        for status in ("completed", "aborted"):
            for field, value in (("subject_id", "another-subject"), ("round", 2)):
                with self.subTest(status=status, field=field):
                    self.directory = self.directory.with_name(f"snapshot-identity-{status}-{field}")
                    self.initialize()
                    self.complete_rows()
                    terminal = self.terminal(status)
                    terminal[field] = value
                    result = self.recorder.finish(terminal, status == "completed")
                    self.assertEqual(result["session_status"], "save_failed")
                    self.assertIn("身份", result["termination_reason"])

    def test_csv_headers_cannot_omit_round_identity_or_add_legacy_day(self):
        for name in ("eeg_raw.csv", "labels.csv"):
            for variant in ("missing-round", "legacy-day"):
                with self.subTest(name=name, variant=variant):
                    self.directory = self.directory.with_name(f"header-{name}-{variant}")
                    self.initialize()
                    self.complete_rows()
                    terminal = self.recorder.finish(self.terminal(), True)
                    self.assertEqual(terminal["session_status"], "completed")
                    rows = self.read_rows(name)
                    fields = list(rows[0])
                    if variant == "missing-round":
                        fields.remove("round")
                    else:
                        fields.append("day")
                    with (self.directory / name).open("w", encoding="utf-8", newline="") as destination:
                        writer = csv.DictWriter(destination, fields, extrasaction="ignore")
                        writer.writeheader()
                        writer.writerows(rows)
                    with self.assertRaisesRegex(OSError, "表头"):
                        self.recorder.verify(terminal, True)

    def test_plan_fingerprint_detects_video_metadata_changes(self):
        mutations = (
            ("missing", lambda snapshot: snapshot.pop("plan_id")),
            ("mismatched", lambda snapshot: snapshot.update(plan_id="0" * 64)),
            ("video-sha", lambda snapshot: snapshot["trials"][0]["video"].update(sha256="b" * 64)),
            ("video-duration", lambda snapshot: snapshot["trials"][0]["video"].update(duration_seconds=8.0)),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                self.directory = self.directory.with_name(f"invalid-plan-{name}")
                self.initialize()
                self.complete_rows()
                terminal = self.terminal()
                mutate(terminal)
                result = self.recorder.finish(terminal, True)
                self.assertEqual(result["session_status"], "save_failed")
                self.assertIn("播放计划指纹", result["termination_reason"])

    def test_valid_fingerprint_cannot_authorize_wrong_round_videos_or_pair_order(self):
        for variant in ("wrong-round", "swapped-pairs", "wrong-path", "wrong-filename"):
            with self.subTest(variant=variant):
                self.directory = self.directory.with_name(f"invalid-media-{variant}")
                self.snapshot = self.planned_snapshot(round=2, first="attention")
                trials = self.snapshot["trials"]
                if variant == "wrong-round":
                    for trial in trials:
                        if trial["condition"] == "attention":
                            index = (trial["trial_order"] - 1) // 2 + 1
                            trial["video"].update(video_id=f"attention_{index:02d}",
                                                  path=f"attention_video/{index:02d}.mp4",
                                                  filename=f"{index:02d}.mp4")
                elif variant == "swapped-pairs":
                    trials[0]["video"], trials[2]["video"] = trials[2]["video"], trials[0]["video"]
                elif variant == "wrong-path":
                    trials[0]["video"]["path"] = "attention_video/09.mp4"
                else:
                    trials[0]["video"]["filename"] = "09.mp4"
                self.refresh_plan_id(self.snapshot)
                self.initialize()
                self.complete_rows()
                result = self.recorder.finish(self.terminal(), True)
                self.assertEqual(result["session_status"], "save_failed")
                self.assertIn("本轮视频素材", result["termination_reason"])

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
        self.assertEqual(recovery_event["subject_id"], self.snapshot["subject_id"])
        self.assertEqual(recovery_event["round"], "1")
        self.assertNotIn("day", recovery_event)
        self.assertEqual(recovery_event["event_monotonic_ns"], "")
        self.assertTrue(json.loads(recovery_event["details_json"])["recovered_on_startup"])
        self.assertEqual(recover_sessions(self.root), [])

    def test_legacy_recovery_retains_old_paths_schemas_headers_and_terminal_records(self):
        legacy_parent = self.root / "sub_legacy" / "day_02" / "round_01"
        legacy_fields = [field for field in LABEL_FIELDS if field not in ("subject_id", "round")]
        terminal_bytes = []
        for status in ("recording", "completed", "aborted", "interrupted", "save_failed"):
            directory = legacy_parent / status
            directory.mkdir(parents=True)
            snapshot = dict(schema_version="1.0", experiment_protocol_id="earwise-3day-1round-4trial-randomstart-v3",
                            session_id=f"legacy-{status}", subject_id="legacy", day=2, round=1,
                            session_status=status, termination_reason="keep original reason")
            atomic_json(directory / "config.json", snapshot)
            (directory / "eeg_raw.csv").write_bytes(b"session_id,sample_row_index\r\nlegacy,0\r\n")
            with (directory / "labels.csv").open("w", encoding="utf-8", newline="") as destination:
                writer = csv.DictWriter(destination, legacy_fields)
                writer.writeheader()
                writer.writerow(dict(session_id=snapshot["session_id"], event_id="legacy-event", event_type="SESSION_START"))
            if status != "recording":
                terminal_bytes.extend((path, path.read_bytes()) for path in directory.iterdir())
        recording = legacy_parent / "recording"
        raw_before = (recording / "eeg_raw.csv").read_bytes()
        labels_before = (recording / "labels.csv").read_bytes()
        self.assertEqual(recover_sessions(self.root), [str(recording)])
        snapshot = json.loads((recording / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["session_status"], "interrupted")
        self.assertEqual(snapshot["schema_version"], "1.0")
        self.assertEqual(snapshot["experiment_protocol_id"], "earwise-3day-1round-4trial-randomstart-v3")
        self.assertEqual((snapshot["day"], snapshot["round"]), (2, 1))
        self.assertEqual((recording / "eeg_raw.csv").read_bytes(), raw_before)
        self.assertTrue((recording / "labels.csv").read_bytes().startswith(labels_before))
        with (recording / "labels.csv").open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            rows = list(reader)
            self.assertEqual(reader.fieldnames, legacy_fields)
        self.assertEqual(len(rows), 2)
        self.assertNotIn(None, rows[-1])
        self.assertEqual(rows[-1]["session_id"], "legacy-recording")
        self.assertEqual(rows[-1]["event_type"], "SESSION_INTERRUPTED")
        for path, data in terminal_bytes:
            self.assertEqual(path.read_bytes(), data)
        self.assertEqual(recover_sessions(self.root), [])


if __name__ == "__main__":
    unittest.main()
