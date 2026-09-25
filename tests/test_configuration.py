import copy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from app.configuration import (
    EXPERIMENT_PROTOCOL_ID, TOTAL_ROUNDS, TRIALS_PER_ROUND, ConfigurationError,
    load_settings, media_catalog, plan_fingerprint, plan_for, randomization_for,
    validate_manifest, validate_subject,
)


ROOT = Path(__file__).resolve().parents[1]


def atom(kind, content):
    return struct.pack(">I4s", len(content) + 8, kind) + content


def movie(seconds=10, version=0, codec=b"avc1"):
    if version == 0:
        header = bytes(12) + struct.pack(">II", 1000, seconds * 1000)
    else:
        header = bytes([1, 0, 0, 0]) + bytes(16) + struct.pack(">IQ", 1000, seconds * 1000)
    sample = atom(b"stsd", bytes(4) + struct.pack(">I", 1) + atom(codec, bytes(20)))
    track = atom(b"trak", atom(b"mdia", atom(b"minf", atom(b"stbl", sample))))
    return atom(b"ftyp", b"isom" + bytes(4)) + atom(b"moov", atom(b"mvhd", header) + track)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        for name in ("settings.json", "video_manifest.json"):
            self.root.joinpath("config", name).write_bytes(ROOT.joinpath("config", name).read_bytes())
        for condition, count in (("attention", 12), ("relax", 4)):
            folder = self.root / f"{condition}_video"
            folder.mkdir()
            for index in range(1, count + 1):
                (folder / f"{index:02d}.mp4").write_bytes(movie(index + 1))

    def edit(self, name, mutate):
        path = self.root / "config" / name
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        mutate(data)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_user_configuration_is_preserved_and_hardware_is_unverified(self):
        settings = load_settings(self.root)
        self.assertEqual(settings["baseline_instruction"], "静息30s")
        self.assertEqual(settings["baseline_seconds"], 30)
        self.assertEqual(settings["channel_mapping"], {"channel_0": None, "channel_1": None, "verified": False})
        self.assertFalse(settings["uv_verified"])
        self.assertTrue(all(value is False for value in settings["validations"].values()))
        self.assertEqual(settings["commands"], ["b"])

    def test_settings_reject_invalid_values_and_unverified_mapping_claim(self):
        original = json.loads((self.root / "config/settings.json").read_text(encoding="utf-8-sig"))
        changes = (("baseline_seconds", 29), ("baseline_seconds", True), ("window_seconds", float("nan")),
                   ("sequence_max_delta", 128), ("sequence_trust_seconds", 2), ("uv_verified", "false"),
                   ("notify_uuid", "oops"), ("commands", ["bb"]), ("baseline_instruction", ""),
                   ("algorithm_versions", {}), ("protocol", {}), ("adc_bits", 16), ("quality_refresh_seconds", 2))
        for key, value in changes:
            with self.subTest(key=key, value=value):
                settings = copy.deepcopy(original)
                settings[key] = value
                (self.root / "config/settings.json").write_text(json.dumps(settings), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    load_settings(self.root)
        original["channel_mapping"]["verified"] = True
        (self.root / "config/settings.json").write_text(json.dumps(original), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "left"):
            load_settings(self.root)

    def test_all_three_rounds_use_four_trials_with_the_exact_round_media(self):
        self.assertEqual(EXPERIMENT_PROTOCOL_ID, "earwise-3round-4trial-randomstart-v4")
        self.assertEqual(TOTAL_ROUNDS, 3)
        self.assertEqual(TRIALS_PER_ROUND, 4)
        manifest = validate_manifest(self.root)
        self.assertEqual(len(manifest), 12)
        self.assertTrue(all("day" not in entry for entry in manifest))
        self.assertEqual({entry["round"] for entry in manifest}, {1, 2, 3})
        attention_ids = []
        for round_number in (1, 2, 3):
            plan = plan_for(self.root, round_number, subject_id="001")
            first = randomization_for("001", round_number)["first_condition"]
            other = "relax" if first == "attention" else "attention"
            self.assertEqual([trial["condition"] for trial in plan], [first, other, first, other])
            self.assertEqual([trial["trial_order"] for trial in plan], [1, 2, 3, 4])
            self.assertEqual(len(plan), 4)
            for condition in ("attention", "relax"):
                matching = [trial for trial in plan if trial["condition"] == condition]
                indices = ((round_number - 1) * 2 + 1, round_number * 2) if condition == "attention" else (1, 2)
                self.assertEqual(len(matching), 2)
                for trial, index in zip(matching, indices):
                    self.assertEqual(trial["video"]["path"], f"{condition}_video/{index:02d}.mp4")
                    self.assertEqual(trial["video"]["video_id"], f"{condition}_{index:02d}")
                    self.assertEqual(trial["video"]["filename"], f"{index:02d}.mp4")
                    if condition == "attention":
                        attention_ids.append(trial["video"]["video_id"])
        self.assertEqual(len(set(attention_ids)), 6)
        self.assertEqual(len(media_catalog(self.root)), 8)

    def test_manifest_file_order_does_not_change_trial_order_or_remove_repeated_conditions(self):
        expected = plan_for(self.root, 1, subject_id="001")
        self.edit("video_manifest.json", lambda data: data["trials"].reverse())
        plan = plan_for(self.root, 1, subject_id="001")
        self.assertEqual(plan, expected)
        self.assertEqual([trial["trial_order"] for trial in plan], [1, 2, 3, 4])
        self.assertEqual([trial["video"]["video_id"] for trial in plan],
                         ["attention_01", "relax_01", "attention_02", "relax_02"])

    def test_random_start_is_stable_for_normalized_identity_and_has_both_directions(self):
        round_one = randomization_for("001", 1)
        self.assertEqual(round_one, {"method": "sha256-subject-round-first-condition-v1",
                                    "seed": "cdf0365debbf5e5d19c670a0bafc21f531ea59eda65df86990484f6f884e2eca",
                                    "first_condition": "attention", "retry_policy": "reuse-subject-round"})
        self.assertEqual(randomization_for(" 001 ", 1), round_one)
        self.assertEqual(plan_for(self.root, 1, subject_id=" 001 "), plan_for(self.root, 1, subject_id="001"))
        self.assertEqual(randomization_for("001", 2)["first_condition"], "attention")
        self.assertEqual(randomization_for("001", 3)["first_condition"], "relax")
        self.assertNotEqual(randomization_for("002", 1)["seed"], round_one["seed"])
        self.assertEqual(len({randomization_for("001", round_number)["seed"] for round_number in (1, 2, 3)}), 3)
        self.assertEqual({randomization_for(str(subject), 1)["first_condition"] for subject in range(20)},
                         {"attention", "relax"})

    def test_case_aliases_share_the_same_windows_identity_randomization_and_plan(self):
        self.assertEqual(randomization_for("abc", 1), randomization_for(" ABC ", 1))
        lower = plan_for(self.root, 1, subject_id="abc")
        upper = plan_for(self.root, 1, subject_id="ABC")
        self.assertEqual(lower, upper)
        self.assertEqual(plan_fingerprint("abc", 1, lower), plan_fingerprint(" ABC ", 1, upper))
        self.assertEqual(validate_subject(" ABC "), "ABC")

    def test_fresh_process_reproduces_the_same_plan_and_randomization(self):
        script = ("import json,sys; from pathlib import Path; "
                  "from app.configuration import plan_for,randomization_for; "
                  "print(json.dumps([randomization_for('001',1),plan_for(Path(sys.argv[1]),1,subject_id='001')]))")
        output = subprocess.check_output([sys.executable, "-c", script, str(self.root)], cwd=ROOT,
                                         text=True, encoding="utf-8")
        self.assertEqual(json.loads(output), [randomization_for("001", 1),
                                            plan_for(self.root, 1, subject_id="001")])

    def test_plan_fingerprint_binds_identity_order_and_all_video_metadata(self):
        plan = plan_for(self.root, 1, subject_id="001")
        fingerprint = plan_fingerprint("001", 1, plan)
        self.assertEqual(len(fingerprint), 64)
        self.assertEqual(plan_fingerprint(" 001 ", 1, copy.deepcopy(plan)), fingerprint)
        self.assertNotEqual(plan_fingerprint("002", 1, plan), fingerprint)
        self.assertNotEqual(plan_fingerprint("001", 2, plan), fingerprint)
        self.assertNotEqual(plan_fingerprint("001", 1, list(reversed(plan))), fingerprint)
        for key in ("sha256", "size_bytes", "mtime_ns", "ctime_ns", "duration_seconds", "path", "video_id"):
            with self.subTest(metadata=key):
                changed = copy.deepcopy(plan)
                changed[0]["video"][key] = "changed"
                self.assertNotEqual(plan_fingerprint("001", 1, changed), fingerprint)

    def test_randomization_rejects_invalid_identity_and_round(self):
        for subject, round_number in (("../x", 1), ("", 1), ("001", 0), ("001", 4), ("001", True), ("001", "1")):
            with self.subTest(subject=subject, round=round_number):
                with self.assertRaises(ConfigurationError):
                    randomization_for(subject, round_number)
                with self.assertRaises(ConfigurationError):
                    plan_fingerprint(subject, round_number, [])
        with self.assertRaises(TypeError):
            plan_for(self.root, 1)

    def test_missing_media_names_the_exact_file(self):
        (self.root / "attention_video/06.mp4").unlink()
        with self.assertRaisesRegex(ConfigurationError, "06.mp4"):
            validate_manifest(self.root)

    def test_catalog_only_requires_the_eight_current_stimuli(self):
        catalog = media_catalog(self.root)
        self.assertEqual([video["video_id"] for video in catalog],
                         [f"attention_{index:02d}" for index in range(1, 7)] +
                         [f"relax_{index:02d}" for index in range(1, 3)])
        for condition, indices in (("attention", range(7, 13)), ("relax", range(3, 5))):
            for index in indices:
                (self.root / f"{condition}_video/{index:02d}.mp4").unlink()
        self.assertEqual(len(validate_manifest(self.root)), 12)
        self.assertEqual(media_catalog(self.root), catalog)
        (self.root / "attention_video/06.mp4").unlink()
        with self.assertRaisesRegex(ConfigurationError, "06.mp4"):
            media_catalog(self.root)

    def test_invalid_manifest_cannot_silently_fall_back(self):
        self.edit("video_manifest.json", lambda data: data["trials"][0]["video"].update(path="../01.mp4"))
        with self.assertRaises(ConfigurationError):
            validate_manifest(self.root)

    def test_duplicate_trial_rejected(self):
        self.edit("video_manifest.json", lambda data: data["trials"].__setitem__(1, data["trials"][0]))
        with self.assertRaisesRegex(ConfigurationError, "重复"):
            validate_manifest(self.root)

    def test_duplicate_trial_order_rejected_even_with_different_condition(self):
        self.edit("video_manifest.json", lambda data: data["trials"][1].update(trial_order=1))
        with self.assertRaisesRegex(ConfigurationError, "重复"):
            validate_manifest(self.root)

    def test_missing_trial_rejected(self):
        self.edit("video_manifest.json", lambda data: data["trials"].pop())
        with self.assertRaisesRegex(ConfigurationError, "12 个 trial"):
            validate_manifest(self.root)

    def test_trial_order_is_required_and_must_be_integer_one_to_four(self):
        original = json.loads((self.root / "config/video_manifest.json").read_text(encoding="utf-8-sig"))
        for value in (None, 0, 5, True, 1.0, "1"):
            with self.subTest(trial_order=value):
                manifest = copy.deepcopy(original)
                manifest["trials"][0]["trial_order"] = value
                (self.root / "config/video_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(ConfigurationError, "trial_order"):
                    validate_manifest(self.root)

    def test_incorrect_condition_sequence_rejected(self):
        self.edit("video_manifest.json", lambda data: data["trials"][2].update(condition="relax"))
        with self.assertRaisesRegex(ConfigurationError, "condition 必须为 attention"):
            validate_manifest(self.root)

    def test_old_manifest_schema_is_rejected(self):
        for schema in ("1.0", "2.0", "3.0"):
            with self.subTest(schema=schema):
                self.edit("video_manifest.json", lambda data: data.update(schema_version=schema))
                with self.assertRaisesRegex(ConfigurationError, "schema_version=4.0"):
                    validate_manifest(self.root)

    def test_new_manifest_cannot_keep_legacy_day_field(self):
        self.edit("video_manifest.json", lambda data: data["trials"][0].update(day=1))
        with self.assertRaisesRegex(ConfigurationError, "不再使用 day"):
            validate_manifest(self.root)

    def test_wrong_source_video_pair_is_rejected(self):
        self.edit("video_manifest.json", lambda data: data["trials"][2]["video"].update(
            filename="01.mp4", video_id="attention_01", path="attention_video/01.mp4"))
        with self.assertRaisesRegex(ConfigurationError, "attention_video/02.mp4"):
            validate_manifest(self.root)

    def test_round_must_be_integer_one_to_three_in_manifest(self):
        for round_number in (None, 0, 4, True, False, "1", 1.0):
            with self.subTest(round=round_number):
                self.edit("video_manifest.json", lambda data: data["trials"][0].update(round=round_number))
                with self.assertRaisesRegex(ConfigurationError, "round 必须为整数 1、2 或 3"):
                    validate_manifest(self.root)

    def test_subject_is_safe_and_preserves_leading_zeroes(self):
        self.assertEqual(validate_subject(" 001 "), "001")
        self.assertEqual(validate_subject("被试_03"), "被试_03")
        for invalid in (None, 1, "", "  ", "../x", "..", "a/b", "a\\b", "a:b", "a*", "x.",
                        "CON", "nul.txt", "COM1", "lpt9.data", "COM¹", "a\x00b", "x" * 65):
            with self.subTest(subject=invalid):
                with self.assertRaises(ConfigurationError):
                    validate_subject(invalid)

    def test_round_cannot_be_coerced_from_unsafe_types(self):
        for round_number in (None, 0, 4, 5, True, False, "1", 1.0):
            with self.subTest(round=round_number):
                with self.assertRaises(ConfigurationError):
                    plan_for(self.root, round_number, subject_id="001")

    def test_old_day_round_plan_signature_is_rejected(self):
        with self.assertRaises(TypeError):
            plan_for(self.root, 1, 1, subject_id="001")
        with self.assertRaises(TypeError):
            plan_for(self.root, day=1, subject_id="001")

    def test_media_metadata_sha256_and_cache_invalidation(self):
        video = next(trial["video"] for trial in plan_for(self.root, 1, subject_id="001")
                     if trial["video"]["video_id"] == "attention_01")
        source = self.root / video["path"]
        self.assertEqual(video["duration_seconds"], 2)
        self.assertEqual(video["codecs"], ["avc1"])
        self.assertEqual(video["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(video["url"], "/media/attention_video/01.mp4")
        self.assertEqual(video["size_bytes"], source.stat().st_size)
        self.assertEqual(video["mtime_ns"], source.stat().st_mtime_ns)
        source.write_bytes(movie(23, version=1, codec=b"av01"))
        changed = next(trial["video"] for trial in plan_for(self.root, 1, subject_id="001")
                       if trial["video"]["video_id"] == "attention_01")
        self.assertEqual(changed["duration_seconds"], 23)
        self.assertEqual(changed["codecs"], ["av01"])
        self.assertNotEqual(changed["sha256"], video["sha256"])

    def test_corrupt_mp4_is_not_treated_as_playable_media(self):
        (self.root / "attention_video/01.mp4").write_bytes(b"not an mp4 file")
        with self.assertRaises(ConfigurationError):
            plan_for(self.root, 1, subject_id="001")


if __name__ == "__main__":
    unittest.main()
