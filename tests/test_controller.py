"""State-machine integration tests using a fake clock and real temporary CSVs."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.controller import Controller, ExperimentError
from app.configuration import EXPERIMENT_PROTOCOL_ID, randomization_for
from app.device import RealDevice
from app.protocol import encode_frame
from app.quality import ALGORITHM_VERSION


PROJECT = Path(__file__).resolve().parents[1]
OWNER = "controller-owner"
OBSERVER = "controller-observer"


class FakeClock:
    def __init__(self):
        self.ns = 1_000_000_000_000

    def __call__(self):
        return self.ns

    def advance(self, seconds):
        self.ns += round(seconds * 1e9)


def mock_catalog():
    return [dict(video_id=f"{condition}_{index:02}", path=f"{condition}_video/{index:02}.mp4",
                 filename=f"{index:02}.mp4", url=f"/media/{condition}_video/{index:02}.mp4",
                 duration_seconds=6.0, sha256="1" * 64,
                 size_bytes=10000, mtime_ns=1000000, ctime_ns=1000000)
            for condition, count in (("attention", 6), ("relax", 2)) for index in range(1, count + 1)]


def mock_plan(_root, rnd, *, subject_id="001"):
    first = randomization_for(subject_id, rnd)["first_condition"]
    order = ("attention", "relax", "attention", "relax") if first == "attention" else ("relax", "attention", "relax", "attention")
    catalog = {video["video_id"]: video for video in mock_catalog()}
    result = []
    for position, condition in enumerate(order):
        index = position // 2 + 1
        if condition == "attention":
            index += (rnd - 1) * 2
        result.append(dict(trial_order=position + 1, condition=condition,
                           video=copy.deepcopy(catalog[f"{condition}_{index:02}"])))
    return result


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mema-controller-test-")
        self.root = Path(self.temporary.name)
        self.clock = FakeClock()
        self.settings = json.loads((PROJECT / "config" / "settings.json").read_text(encoding="utf-8-sig"))
        self.patches = [patch("app.controller.load_settings", return_value=copy.deepcopy(self.settings)),
                        patch("app.controller.validate_manifest", return_value=[]),
                        patch("app.controller.media_catalog", side_effect=lambda root: mock_catalog()),
                        patch("app.controller.plan_for", side_effect=mock_plan)]
        for item in self.patches:
            item.start()
        self.controller = Controller(self.root, simulate=True, clock=self.clock)
        await self.controller.initialize()
        self.sequence = 0
        self.requests = 0
        self.event_index = 0
        self.controller.on_status("connected", "模拟测试")
        self.feed()
        await self.controller.action("browser_probe", {"client_id": OWNER, "videos": [
            {"video_id": video["video_id"], "playable": True, "duration_seconds": video["duration_seconds"]}
            for video in mock_catalog()]})
        self.sync_offset_ns = self.clock() - 1000_000_000
        await self.controller.action("sync", dict(client_id=OWNER, client_midpoint_ms=1000,
                                                  server_monotonic_ns=self.clock(), rtt_ms=4))

    async def asyncTearDown(self):
        await self.controller.close()
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def feed(self):
        self.controller.on_data(encode_frame(self.sequence, 100 + self.sequence, -200 - self.sequence))
        self.sequence = (self.sequence + 1) % 256

    def body(self, **kwargs):
        result = dict(client_id=OWNER)
        if self.controller.session:
            result["session_id"] = self.controller.session["session_id"]
        result.update(kwargs)
        return result

    async def start(self, rnd=1, **extra):
        self.requests += 1
        self.feed()
        body = self.body(subject_id="001", round=rnd, request_id=f"request-{self.requests:04}", audio_confirmed=True)
        body.update(extra)
        preflight = await self.controller.action("preflight", body)
        body.setdefault("plan_id", preflight["plan_id"])
        result = await self.controller.action("start", body)
        return result, body

    async def advance(self, seconds, *, with_data=True, heartbeat=True):
        remaining = seconds
        while remaining > 1e-8:
            step = min(.25, remaining)
            self.clock.advance(step)
            remaining -= step
            if with_data:
                self.feed()
            if heartbeat and self.controller.recording:
                await self.controller.action("heartbeat", self.body())
            await self.controller.tick()

    def media_body(self, kind, position, **extra):
        self.event_index += 1
        result = self.body(trial_id=self.controller.session["current_trial"]["trial_id"],
                           event_type=kind, media_position_ms=position * 1000,
                           client_performance_ms=(self.clock()-self.sync_offset_ns)/1e6,
                           playback_rate=1, client_event_id=f"media-{self.event_index}")
        result.update(extra)
        return result

    async def media(self, kind, position, **extra):
        body = self.media_body(kind, position, **extra)
        await self.controller.action("media", body)
        return body

    def read_outputs(self, directory=None):
        directory = Path(directory or self.controller.session["output_directory"])
        with (directory / "eeg_raw.csv").open(encoding="utf-8", newline="") as source:
            eeg = list(csv.DictReader(source))
        with (directory / "labels.csv").open(encoding="utf-8", newline="") as source:
            labels = list(csv.DictReader(source))
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        return eeg, labels, config

    async def complete_trial(self, score=3):
        await self.media("playing", 0)
        await self.advance(6)
        await self.media("ended", 6)
        rating = self.body(trial_id=self.controller.session["current_trial"]["trial_id"], score=score, confidence_score=4)
        await self.controller.action("rating", rating)
        return rating

    async def use_real_controller(self, *, connected=True):
        """Exercise the real-mode pipeline with injected frames, without BLE hardware."""
        await self.controller.close()
        settings = copy.deepcopy(self.settings)
        settings["channel_mapping"] = {"channel_0": None, "channel_1": None, "verified": False}
        settings["validations"] = {key: False for key in (
            "commands_verified", "sample_rate_verified", "saturation_verified")}
        settings["uv_verified"] = False
        settings["nominal_sample_rate"] = 250
        with patch("app.controller.load_settings", return_value=settings):
            self.controller = Controller(self.root, simulate=False, clock=self.clock)
        await self.controller.initialize()
        self.assertEqual(self.controller.mode, "real")
        self.assertIsInstance(self.controller.device, RealDevice)
        if connected:
            self.controller.device._set_status("connected", "测试注入连接状态；未调用蓝牙连接")
            self.feed()
        await self.controller.action("browser_probe", {"client_id": OWNER, "videos": [
            {"video_id": video["video_id"], "playable": True, "duration_seconds": video["duration_seconds"]}
            for video in mock_catalog()]})
        await self.controller.action("sync", dict(client_id=OWNER, client_midpoint_ms=1000,
                                                  server_monotonic_ns=self.clock(), rtt_ms=4))
        self.sync_offset_ns = self.clock() - 1000_000_000

    async def test_both_random_start_orders_have_exact_three_auditable_files(self):
        cases = [(rnd, order) for rnd in (1, 2, 3) for order in (
            ["attention", "relax", "attention", "relax"], ["relax", "attention", "relax", "attention"])]
        for rnd, order in cases:
            subject = next(f"{n:03}" for n in range(100)
                           if randomization_for(f"{n:03}", rnd)["first_condition"] == order[0])
            with self.subTest(round=rnd, first=order[0]):
                preview = await self.controller.action("preflight", self.body(subject_id=subject, round=rnd))
                await self.start(rnd, subject_id=subject)
                await self.advance(29.75)
                self.assertEqual(self.controller.session["stage"], "baseline")
                await self.advance(.25)
                self.assertEqual(self.controller.session["stage"], "transition")
                self.assertEqual([trial["condition"] for trial in self.controller.session["trials"]], order)
                scores = (2, 5, 1, 4)
                for index, score in enumerate(scores):
                    await self.complete_trial(score)
                    if index < 3:
                        self.assertTrue(self.controller.recording)
                        self.assertEqual(self.controller.session["stage"], "transition")
                        self.assertEqual(self.controller.session["trial_index"], index + 1)
                self.assertEqual(self.controller.session["status"], "completed")
                directory = Path(self.controller.session["output_directory"])
                self.assertEqual({p.name for p in directory.iterdir()}, {"eeg_raw.csv", "labels.csv", "config.json"})
                eeg, labels, config = self.read_outputs()
                self.assertEqual([int(row["sample_row_index"]) for row in eeg], list(range(len(eeg))))
                self.assertTrue(all(row["channel_0_uv"] == row["channel_1_uv"] == "" for row in eeg))
                self.assertEqual(config["mode"], "simulation")
                self.assertEqual(config["subject_id"], subject)
                self.assertEqual(config["round"], rnd)
                self.assertNotIn("day", config)
                self.assertNotIn("day", self.controller.session)
                self.assertEqual(config["schema_version"], "2.0")
                self.assertEqual(directory.parent, self.controller.data_root / f"sub_{subject}" / f"round_{rnd:02}")
                self.assertTrue(all(row["round"] == str(rnd) and row["subject_id"] == subject for row in eeg + labels))
                self.assertNotIn("day", eeg[0])
                self.assertNotIn("day", labels[0])
                self.assertEqual(config["plan_id"], preview["plan_id"])
                self.assertEqual(config["randomization"], preview["randomization"])
                self.assertEqual([t["video"] for t in config["trials"]], [t["video"] for t in preview["plan"]])
                self.assertEqual(config["summary"]["actual_rows"], len(eeg))
                self.assertEqual(config["summary"]["questionnaires_submitted"], 4)
                self.assertEqual(config["experiment_protocol_id"], EXPERIMENT_PROTOCOL_ID)
                self.assertEqual([t["trial_order"] for t in config["trials"]], [1, 2, 3, 4])
                self.assertEqual(len({t["trial_id"] for t in config["trials"]}), 4)
                self.assertTrue(all(t["completed"] and t["rating_submitted"] for t in config["trials"]))
                for kind, count in (("BASELINE_START", 1), ("BASELINE_END", 1),
                                    ("TRIAL_START", 4), ("TRIAL_END", 4), ("RATING_SUBMITTED", 4)):
                    self.assertEqual(sum(row["event_type"] == kind for row in labels), count)
                baseline_start = next(row for row in labels if row["event_type"] == "BASELINE_START")
                baseline_end = next(row for row in labels if row["event_type"] == "BASELINE_END")
                self.assertEqual(int(baseline_end["event_monotonic_ns"]) - int(baseline_start["event_monotonic_ns"]), 30_000_000_000)
                self.assertLess(len(eeg), 7500)  # Baseline obeys elapsed time, never waits for a sample quota.
                ratings = [row for row in labels if row["event_type"] == "RATING_SUBMITTED"]
                self.assertEqual([row["condition"] for row in ratings], order)
                for index, row in enumerate(ratings):
                    self.assertEqual(row[f"{row['condition']}_score"], str(scores[index]))
                    self.assertEqual(row["trial_id"], config["trials"][index]["trial_id"])
                    self.assertEqual(row["video_id"], config["trials"][index]["video"]["video_id"])
                    self.assertEqual(row["relax_score" if row["condition"] == "attention" else "attention_score"], "")
                    self.assertEqual(row["confidence_score"], "4")

    async def test_only_rounds_one_to_three_are_accepted(self):
        for rnd in (0, 4, 5, True, 1.0, "1", None):
            with self.subTest(round=rnd):
                with self.assertRaises(ExperimentError):
                    await self.controller.action("preflight", self.body(subject_id="001", round=rnd))
                with self.assertRaises(ExperimentError):
                    await self.start(rnd=rnd)
        self.assertIsNone(self.controller.recorder)

    async def test_round_is_required_and_preview_survives_page_change(self):
        for identity in (dict(subject_id="001"), dict(subject_id="001", day=1),
                         dict(subject_id="001", day=1, round=2)):
            with self.subTest(identity=identity), self.assertRaises(ExperimentError):
                await self.controller.action("preflight", self.body(**identity))
        identity = dict(subject_id="001", round=1)
        preview = await self.controller.action("preflight", self.body(**identity))
        repeated = await self.controller.action("preflight", self.body(**identity))
        refreshed = await self.controller.action("preflight", dict(client_id=OBSERVER, **identity))
        self.assertEqual(preview, repeated)
        self.assertEqual(preview, refreshed)
        self.assertIsNone(self.controller.recorder)
        await self.controller.action("start", self.body(**identity, request_id="round-identity",
                                     plan_id=preview["plan_id"], audio_confirmed=True))
        self.assertEqual(self.controller.session["round"], 1)
        self.assertEqual(self.controller.session["plan_id"], preview["plan_id"])
        self.assertEqual(self.controller.session["randomization"], preview["randomization"])

    async def test_missing_stale_or_other_identity_plan_cannot_start(self):
        preview = await self.controller.action("preflight", self.body(subject_id="001", round=1))
        base = self.body(subject_id="001", round=1, request_id="plan-check-start",
                         audio_confirmed=True, plan_id=preview["plan_id"])
        for change in ({"plan_id": None}, {"plan_id": "wrong-plan"},
                       {"subject_id": "002"}, {"round": 2}):
            with self.subTest(change=change), self.assertRaisesRegex(ExperimentError, "计划"):
                await self.controller.action("start", {**base, **change})
        changed = mock_plan(self.root, 1, subject_id="001")
        changed[0]["video"]["sha256"] = "2" * 64
        with patch("app.controller.plan_for", return_value=changed):
            with self.assertRaisesRegex(ExperimentError, "计划"):
                await self.controller.action("start", base)
        self.assertIsNone(self.controller.recorder)
        self.assertFalse(list(self.controller.data_root.glob("sub_*/round_*/*/config.json")))

    async def test_previous_two_round_protocol_is_kept_but_not_counted(self):
        directory = self.controller.data_root / "sub_001/day_01/round_01/old-v2"
        directory.mkdir(parents=True)
        path = directory / "config.json"
        path.write_text(json.dumps(dict(session_id="old-v2", session_status="completed",
                        experiment_protocol_id="earwise-3day-2round-4trial-v2")), encoding="utf-8")
        before = path.read_bytes()
        preview = await self.controller.action("preflight", self.body(subject_id="001", round=1))
        self.assertEqual(preview["previous_attempts"], [])
        await self.start()
        await self.controller.action("abort", self.body())
        _, _, saved = self.read_outputs()
        self.assertEqual(saved["attempt_number"], 1)
        self.assertIsNone(saved["retry_of_session_id"])
        self.assertEqual(path.read_bytes(), before)

    async def test_old_day_layout_is_preserved_and_new_round_has_its_own_directory(self):
        old_directory = self.controller.data_root / "sub_001/day_02/round_01/old-v3"
        old_directory.mkdir(parents=True)
        old_config = old_directory / "config.json"
        old_config.write_text(json.dumps(dict(session_id="old-v3", session_status="completed",
                              subject_id="001", day=2, round=1, schema_version="1.0",
                              experiment_protocol_id="earwise-3day-1round-4trial-randomstart-v3")), encoding="utf-8")
        before = old_config.read_bytes()
        preview = await self.controller.action("preflight", self.body(subject_id="001", round=2))
        self.assertEqual(preview["previous_attempts"], [])
        await self.start(2)
        self.feed()
        await self.controller.action("abort", self.body())
        eeg, labels, saved = self.read_outputs()
        self.assertEqual(saved["round"], 2)
        self.assertNotIn("day", saved)
        self.assertEqual(saved["attempt_number"], 1)
        self.assertTrue(all(row["round"] == "2" for row in eeg + labels))
        self.assertEqual(Path(self.controller.session["output_directory"]).parent,
                         self.controller.data_root / "sub_001/round_02")
        self.assertEqual(old_config.read_bytes(), before)

    async def test_attempt_listing_rejects_records_belonging_to_another_round(self):
        parent = self.controller.data_root / "sub_001/round_02"
        for name, changes in (("wrong-round", {"round": 1}), ("wrong-subject", {"subject_id": "002"}),
                              ("legacy-field", {"day": 2}), ("invalid-subject", {"subject_id": 1})):
            directory = parent / name
            directory.mkdir(parents=True)
            metadata = dict(schema_version="2.0", experiment_protocol_id=EXPERIMENT_PROTOCOL_ID,
                            subject_id="001", round=2, session_id=name, session_status="completed")
            metadata.update(changes)
            (directory / "config.json").write_text(json.dumps(metadata), encoding="utf-8")
        self.assertEqual(self.controller.attempts("001", 2), [])

    async def test_old_two_trial_completion_does_not_block_new_protocol(self):
        directory = self.controller.data_root / "sub_001/day_01/round_01/legacy-two-trial"
        directory.mkdir(parents=True)
        legacy = {"session_id": "legacy-two-trial", "session_status": "completed", "attempt_number": 1,
                  "started_at_utc": "2026-09-01T00:00:00+00:00", "trials": mock_plan(self.root, 1)[:2]}
        legacy_path = directory / "config.json"
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        before = legacy_path.read_bytes()
        preflight = await self.controller.action("preflight", self.body(subject_id="001", round=1))
        self.assertEqual(preflight["previous_attempts"], [])
        self.assertEqual(len(preflight["plan"]), 4)
        await self.start()
        await self.controller.action("abort", self.body())
        _, _, config = self.read_outputs()
        self.assertEqual(config["attempt_number"], 1)
        self.assertIsNone(config["retry_of_session_id"])
        self.assertEqual(legacy_path.read_bytes(), before)

    async def test_two_trials_are_only_half_a_round_and_abort_preserves_them(self):
        await self.start()
        await self.advance(30)
        first_rating = await self.complete_trial(2)
        await self.complete_trial(5)
        third = self.controller.session["current_trial"]
        self.assertEqual(third["trial_order"], 3)
        self.assertEqual(third["condition"], self.controller.session["trials"][0]["condition"])
        self.assertTrue(self.controller.recording)
        # Retrying a past rating must not submit the new trial of the same condition.
        await self.controller.action("rating", first_rating)
        self.assertFalse(third["rating_submitted"])
        fourth_id = self.controller.session["trials"][3]["trial_id"]
        with self.assertRaises(ExperimentError):
            await self.media("playing", 0, trial_id=fourth_id)
        await self.controller.action("abort", self.body())
        _, labels, config = self.read_outputs()
        self.assertEqual(config["session_status"], "aborted")
        self.assertEqual(config["summary"]["questionnaires_submitted"], 2)
        self.assertEqual([trial["rating_submitted"] for trial in config["trials"]], [True, True, False, False])
        self.assertEqual(sum(row["event_type"] == "BASELINE_START" for row in labels), 1)
        self.assertEqual(sum(row["event_type"] == "RATING_SUBMITTED" for row in labels), 2)

    async def test_outdated_two_trial_plan_cannot_start(self):
        with patch("app.controller.plan_for", return_value=mock_plan(self.root, 1)[:2]):
            with self.assertRaisesRegex(ExperimentError, "4.*trial"):
                await self.start()
        self.assertFalse(self.controller.recording)
        self.assertIsNone(self.controller.recorder)

    async def test_formal_start_resets_preview_counters_and_rejects_parallel_owner(self):
        for _ in range(20):
            self.feed()
        result, request = await self.start()
        self.assertEqual(self.controller.row_index, 0)
        self.assertEqual(self.controller.quality.totals["received_frames"], 0)
        self.assertEqual(await self.controller.action("start", request), result)
        with self.assertRaises(ExperimentError):
            await self.controller.action("start", {**request, "request_id": "another-start", "client_id": OBSERVER})
        self.feed()
        await self.controller.action("abort", self.body())
        eeg, _, _ = self.read_outputs()
        self.assertEqual(len(eeg), 1)

    async def test_selected_media_changed_since_browser_probe_cannot_start(self):
        for field, value in (("size_bytes", 10001), ("mtime_ns", 1000001),
                             ("ctime_ns", 1000001), ("duration_seconds", 7.0)):
            with self.subTest(field=field):
                changed = mock_plan(self.root, 1)
                changed[0]["video"][field] = value
                with patch("app.controller.plan_for", return_value=changed):
                    with self.assertRaisesRegex(ExperimentError, "素材.*发生变化"):
                        await self.start()
                self.assertFalse(self.controller.recording)
                self.assertIsNone(self.controller.recorder)
                self.assertEqual(list(self.controller.data_root.glob("sub_*/round_*/*/config.json")), [])

    async def test_connected_without_first_frame_is_waiting_and_not_ready(self):
        self.controller.on_status("disconnected", "测试断开")
        self.controller.on_status("connected", "已连接但尚未收到有效帧")
        state = self.controller.state()
        self.assertEqual(state["device"]["status"], "connected")
        self.assertEqual(state["quality"]["data_status"], "waiting")
        self.assertIsNone(state["quality"]["loss_pct"])
        self.assertIn("等待近期有效脑电数据", state["readiness_errors"])
        with self.assertRaises(ExperimentError):
            await self.controller.action("start", self.body(subject_id="001", round=1,
                                                           request_id="waiting-start", audio_confirmed=True))

    async def test_connect_and_command_serialize_with_start_without_blocking_idle_tick(self):
        import asyncio
        from unittest.mock import AsyncMock
        for operation in ("connect", "command"):
            with self.subTest(operation=operation):
                entered, release = asyncio.Event(), asyncio.Event()

                async def blocked_device_operation(*args):
                    entered.set()
                    await release.wait()

                with patch.object(self.controller.device, operation, new=AsyncMock(side_effect=blocked_device_operation)):
                    operation_task = asyncio.create_task(self.controller.action(operation, self.body(command="b")))
                    start_task = None
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=1)
                        start_task = asyncio.create_task(self.start())
                        await asyncio.sleep(.01)
                        self.assertFalse(start_task.done())
                        self.assertFalse(self.controller.recording)
                        # The UI publish loop must remain able to tick during a BLE scan.
                        await asyncio.wait_for(self.controller.tick(), timeout=.2)
                        release.set()
                        await asyncio.wait_for(operation_task, timeout=1)
                        await asyncio.wait_for(start_task, timeout=2)
                        self.assertTrue(self.controller.recording)
                    finally:
                        release.set()
                        tasks = [operation_task] + ([start_task] if start_task else [])
                        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
                await self.controller.action("abort", self.body())

    async def test_real_mode_unverified_metadata_allows_complete_round_without_simulation_fallback(self):
        await self.use_real_controller()
        self.assertEqual(self.controller.readiness_errors(), [])
        await self.start(2, subject_id="real-test")
        # Known bytes travel through the actual RealDevice notification callback,
        # parser, controller and disk writer; no simulator is ever connected.
        injected_frame = encode_frame(self.sequence, 123456, -654321)
        self.controller.device._on_notification(None, injected_frame)
        self.sequence = (self.sequence + 1) % 256
        await self.advance(29.75)
        self.assertEqual(self.controller.session["stage"], "baseline")
        await self.advance(.25)
        self.assertEqual(self.controller.session["stage"], "transition")
        scores = (2, 5, 1, 4)
        for score in scores:
            await self.complete_trial(score)
        self.assertEqual(self.controller.session["status"], "completed")
        self.assertIsInstance(self.controller.device, RealDevice)
        directory = Path(self.controller.session["output_directory"])
        self.assertEqual(directory.parent, self.root / "data" / "sub_real-test" / "round_02")
        self.assertEqual({p.name for p in directory.iterdir()}, {"eeg_raw.csv", "labels.csv", "config.json"})
        eeg, labels, config = self.read_outputs()
        self.assertEqual(config["mode"], "real")
        self.assertEqual(config["session_status"], "completed")
        self.assertEqual(config["nominal_sample_rate"], 250)
        self.assertEqual(config["channel_mapping"], {"channel_0": None, "channel_1": None, "verified": False})
        self.assertEqual(config["validations"], {"commands_verified": False, "sample_rate_verified": False,
                                                "saturation_verified": False})
        self.assertFalse(config["uv_verified"])
        self.assertEqual(config["commands"], ["b"])
        self.assertGreater(len(eeg), 1)
        self.assertEqual((eeg[0]["channel_0_raw"], eeg[0]["channel_1_raw"]), ("123456", "-654321"))
        self.assertEqual(eeg[0]["original_frame_hex"], injected_frame.hex())
        self.assertTrue(all(int(row["channel_0_raw"]) == 100 + int(row["device_seq"])
                            and int(row["channel_1_raw"]) == -200 - int(row["device_seq"])
                            for row in eeg[1:]))
        self.assertTrue(all(row["channel_0_uv"] == row["channel_1_uv"] == "" for row in eeg))
        self.assertTrue(all(row["subject_id"] == "real-test" and row["round"] == "2" for row in eeg + labels))
        self.assertEqual(config["round"], 2)
        self.assertEqual(config["subject_id"], "real-test")
        self.assertNotIn("day", config)
        self.assertEqual(config["summary"]["actual_rows"], len(eeg))
        self.assertEqual(config["summary"]["questionnaires_submitted"], 4)
        self.assertTrue(all(trial["completed"] and trial["rating_submitted"] for trial in config["trials"]))
        for event, count in (("BASELINE_START", 1), ("BASELINE_END", 1), ("TRIAL_START", 4),
                             ("TRIAL_END", 4), ("RATING_SUBMITTED", 4)):
            self.assertEqual(sum(row["event_type"] == event for row in labels), count)
        baseline_start = next(row for row in labels if row["event_type"] == "BASELINE_START")
        baseline_end = next(row for row in labels if row["event_type"] == "BASELINE_END")
        self.assertEqual(int(baseline_end["event_monotonic_ns"]) - int(baseline_start["event_monotonic_ns"]),
                         30_000_000_000)
        ratings = [row for row in labels if row["event_type"] == "RATING_SUBMITTED"]
        for index, row in enumerate(ratings):
            trial = config["trials"][index]
            self.assertEqual((row["condition"], row["trial_id"], row["video_id"]),
                             (trial["condition"], trial["trial_id"], trial["video"]["video_id"]))
            self.assertEqual(row[f"{row['condition']}_score"], str(scores[index]))

    async def test_real_mode_still_requires_connection_recent_frames_and_usable_storage(self):
        await self.use_real_controller(connected=False)
        preview = await self.controller.action("preflight", self.body(subject_id="001", round=1))
        request = self.body(subject_id="001", round=1, request_id="real-readiness-start",
                            audio_confirmed=True, plan_id=preview["plan_id"])

        async def assert_blocked(message):
            with self.assertRaisesRegex(ExperimentError, message):
                await self.controller.action("start", request)
            self.assertIsNone(self.controller.session)
            self.assertIsNone(self.controller.recorder)
            self.assertFalse(list(self.controller.data_root.rglob("config.json")))

        await assert_blocked("请先连接耳机")
        self.controller.device._set_status("connected", "测试连接无数据")
        await assert_blocked("等待近期有效脑电数据")
        self.feed()
        self.clock.advance(self.settings["data_timeout_seconds"] + .01)
        await assert_blocked("等待近期有效脑电数据")
        self.feed()
        self.controller.storage_error = "输出目录不可写：测试权限错误"
        await assert_blocked("输出目录不可写")
        self.controller.storage_error = None
        self.controller.preflight_errors = ["素材配置检查失败：测试无效视频"]
        await assert_blocked("素材配置检查失败")
        self.controller.preflight_errors = []
        self.assertEqual(self.controller.readiness_errors(), [])

    async def test_real_b_command_does_not_start_or_create_recording_files(self):
        await self.use_real_controller(connected=False)
        self.controller.device._set_status("connected", "测试连接无数据")
        with patch.object(self.controller.device, "command", new=AsyncMock()) as command:
            await self.controller.action("command", self.body(command="b"))
        command.assert_awaited_once_with("b")
        self.assertIsNone(self.controller.session)
        self.assertIsNone(self.controller.recorder)
        self.assertFalse(list(self.controller.data_root.rglob("config.json")))
        self.assertIn("等待近期有效脑电数据", self.controller.readiness_errors())
        self.controller.device._on_notification(None, encode_frame(1, 100, -200))
        self.assertEqual(self.controller.readiness_errors(), [])
        self.assertIsNone(self.controller.session)
        self.assertFalse(list(self.controller.data_root.rglob("config.json")))
        self.assertIsInstance(self.controller.device, RealDevice)

    async def test_first_config_already_contains_start_clock_for_crash_recovery(self):
        await self.start()
        config = json.loads((Path(self.controller.session["output_directory"]) / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["started_monotonic_ns"], self.controller.session["started_monotonic_ns"])
        self.assertEqual(config["current_stage"], "baseline")

    async def test_read_only_heartbeat_cannot_keep_owner_session_alive(self):
        await self.start()
        with self.assertRaises(ExperimentError):
            await self.controller.action("heartbeat", self.body(client_id=OBSERVER))
        await self.advance(3.25, heartbeat=False)
        self.assertEqual(self.controller.session["status"], "interrupted")
        self.assertIn("心跳", self.controller.session["reason"])

    async def test_refresh_owner_interrupts_but_observer_unload_does_not(self):
        await self.start()
        await self.controller.action("unload", self.body(client_id=OBSERVER))
        self.assertTrue(self.controller.recording)
        await self.controller.action("unload", self.body())
        self.assertEqual(self.controller.session["status"], "interrupted")
        self.assertIn("刷新或关闭", self.controller.session["reason"])

    async def test_data_timeout_interrupts_and_new_attempt_uses_new_directory(self):
        result, _ = await self.start()
        await self.advance(1.25, with_data=False)
        self.assertEqual(self.controller.session["status"], "interrupted")
        old_directory = self.controller.session["output_directory"]
        self.feed()
        next_result, _ = await self.start()
        self.assertNotEqual(result["session_id"], next_result["session_id"])
        self.assertNotEqual(old_directory, self.controller.session["output_directory"])
        await self.controller.action("abort", self.body())
        _, old_labels, old_config = self.read_outputs(old_directory)
        _, _, new_config = self.read_outputs()
        self.assertEqual(old_config["session_status"], "interrupted")
        self.assertIn("DATA_TIMEOUT", [row["event_type"] for row in old_labels])
        self.assertEqual(new_config["retry_of_session_id"], result["session_id"])
        self.assertEqual(new_config["attempt_number"], 2)

    async def test_late_frame_cannot_hide_timeout_between_watchdog_ticks(self):
        await self.start()
        self.feed()
        self.clock.advance(1.1)
        # Notification arrives before the next periodic watchdog iteration.
        self.feed()
        import asyncio
        for _ in range(20):
            await asyncio.sleep(.01)
            if self.controller.session["status"] == "interrupted":
                break
        self.assertEqual(self.controller.session["status"], "interrupted")
        _, labels, config = self.read_outputs()
        self.assertEqual(config["session_status"], "interrupted")
        self.assertIn("DATA_GAP", [row["event_type"] for row in labels])

    async def test_first_formal_frame_cannot_hide_initial_data_timeout(self):
        await self.start()
        self.clock.advance(1.1)
        self.feed()
        import asyncio
        for _ in range(20):
            await asyncio.sleep(.01)
            if self.controller.session["status"] == "interrupted":
                break
        self.assertEqual(self.controller.session["status"], "interrupted")

    async def test_late_owner_heartbeat_does_not_hide_page_gap(self):
        await self.start()
        for _ in range(13):
            self.clock.advance(.25)
            self.feed()
        # Live EEG continues, but the controlling page has been absent >3 s.
        await self.controller.action("heartbeat", self.body())
        await self.controller.tick()
        self.assertEqual(self.controller.session["status"], "interrupted")
        self.assertIn("心跳", self.controller.session["reason"])

    async def test_bluetooth_disconnect_stops_session_and_clears_partial_frame(self):
        await self.start()
        self.feed()
        self.controller.on_data(encode_frame(99, 1, 2)[:12])
        self.assertGreater(self.controller.parser.buffered_bytes, 0)
        self.controller.on_status("disconnected", "模拟硬件连接丢失")
        self.assertEqual(self.controller.parser.buffered_bytes, 0)
        import asyncio
        for _ in range(20):
            await asyncio.sleep(.01)
            if self.controller.session["status"] == "interrupted":
                break
        self.assertEqual(self.controller.session["status"], "interrupted")
        _, labels, _ = self.read_outputs()
        self.assertIn("BLE_DISCONNECTED", [row["event_type"] for row in labels])

    async def test_media_requires_baseline_and_real_elapsed_duration_and_original_rate(self):
        await self.start()
        with self.assertRaises(ExperimentError):
            await self.controller.action("media", self.body(trial_id="forged", event_type="playing"))
        await self.advance(30)
        with self.assertRaises(ExperimentError):
            await self.media("playing", 0, playback_rate=2)
        await self.media("playing", 0)
        await self.advance(2)
        with self.assertRaises(ExperimentError):
            await self.media("ended", 6)
        # A rejected forged event cannot alter the legitimate player's timeline.
        await self.advance(4)
        await self.media("ended", 6)
        self.assertEqual(self.controller.session["stage"], "questionnaire")

    async def test_media_time_mapping_deduplication_and_buffering_are_recorded(self):
        await self.start()
        await self.advance(30)
        expected_ns = self.clock() - 100_000_000
        playing = await self.media("playing", 0, client_performance_ms=(expected_ns-self.sync_offset_ns)/1e6)
        await self.controller.action("media", playing)
        await self.advance(2)
        await self.media("waiting", 2)
        self.assertEqual(self.controller.session["stage"], "video_buffering")
        await self.advance(.5)
        await self.media("playing", 2)
        await self.advance(4)
        await self.media("ended", 6)
        await self.controller.action("abort", self.body())
        _, labels, _ = self.read_outputs()
        starts = [row for row in labels if row["event_type"] == "TRIAL_START"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(int(starts[0]["event_monotonic_ns"]), expected_ns)
        self.assertEqual(int(starts[0]["server_received_monotonic_ns"]), expected_ns + 100_000_000)
        self.assertEqual(float(starts[0]["sync_uncertainty_ms"]), 2)
        self.assertIn("VIDEO_WAITING", [row["event_type"] for row in labels])
        self.assertIn("video_buffering", [row["stage"] for row in labels])

    async def test_sustained_video_stall_interrupts(self):
        await self.start()
        await self.advance(30)
        await self.media("playing", 0)
        await self.advance(.5)
        await self.media("waiting", .5)
        await self.advance(5.25)
        self.assertEqual(self.controller.session["status"], "interrupted")
        self.assertIn("视频", self.controller.session["reason"])

    async def test_ratings_validate_integer_range_and_are_idempotent(self):
        await self.start()
        await self.advance(30)
        await self.media("playing", 0)
        await self.advance(6)
        await self.media("ended", 6)
        trial_id = self.controller.session["current_trial"]["trial_id"]
        for score, confidence in [(0, 3), (6, 3), (True, 3), (3.0, 2), (3, None)]:
            with self.assertRaises(ExperimentError):
                await self.controller.action("rating", self.body(trial_id=trial_id, score=score, confidence_score=confidence))
        rating = self.body(trial_id=trial_id, score=2, confidence_score=5)
        await self.controller.action("rating", rating)
        await self.controller.action("rating", rating)
        self.assertEqual(self.controller.session["trial_index"], 1)
        with self.assertRaises(ExperimentError):
            await self.controller.action("rating", {**rating, "score": 4})
        await self.controller.action("abort", self.body())
        _, labels, _ = self.read_outputs()
        self.assertEqual(len([row for row in labels if row["event_type"] == "RATING_SUBMITTED"]), 1)

    async def test_completed_retry_requires_confirmation_and_preserves_old_files(self):
        first, _ = await self.start()
        await self.advance(30)
        for _ in range(4):
            await self.complete_trial()
        old_directory = Path(self.controller.session["output_directory"])
        old_bytes = {path.name: path.read_bytes() for path in old_directory.iterdir()}
        with self.assertRaises(ExperimentError):
            await self.start()
        second, _ = await self.start(confirm_retry=True)
        self.assertNotEqual(first["session_id"], second["session_id"])
        await self.controller.action("abort", self.body())
        self.assertEqual({path.name: path.read_bytes() for path in old_directory.iterdir()}, old_bytes)

    async def test_write_failure_stops_and_cannot_be_reported_completed(self):
        await self.start()
        self.controller.recorder._fail(OSError("simulated disk full"))
        # Recorder errors are handed back from its worker via loop.call_soon_threadsafe.
        import asyncio
        for _ in range(20):
            await asyncio.sleep(.01)
            if self.controller.session["status"] == "save_failed":
                break
        self.assertEqual(self.controller.session["status"], "save_failed")
        _, _, config = self.read_outputs()
        self.assertEqual(config["session_status"], "save_failed")
        self.assertIn("disk full", config["termination_reason"])

    async def test_quality_snapshot_algorithm_matches_config_snapshot(self):
        await self.start()
        await self.advance(1)
        await self.controller.action("abort", self.body())
        _, labels, config = self.read_outputs()
        quality = json.loads(next(row["details_json"] for row in labels if row["event_type"] == "QUALITY_SNAPSHOT"))
        self.assertEqual(quality["algorithm_version"], ALGORITHM_VERSION)
        self.assertEqual(config["algorithm_versions"]["packet_loss"], quality["algorithm_version"])


if __name__ == "__main__":
    unittest.main()
