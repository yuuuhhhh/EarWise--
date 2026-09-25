import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from app.configuration import (
    EXPERIMENT_PROTOCOL_ID, ROUNDS_PER_DAY, TRIALS_PER_ROUND, ConfigurationError,
    load_settings, media_catalog, plan_for, validate_manifest, validate_subject,
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

    def test_all_six_rounds_use_four_trials_in_correct_order_with_exact_media(self):
        self.assertEqual(EXPERIMENT_PROTOCOL_ID, "earwise-3day-2round-4trial-v2")
        self.assertEqual(ROUNDS_PER_DAY, 2)
        self.assertEqual(TRIALS_PER_ROUND, 4)
        self.assertEqual(len(validate_manifest(self.root)), 24)
        attention_ids = []
        for day in (1, 2, 3):
            for round_number in (1, 2):
                plan = plan_for(self.root, day, round_number)
                expected = ["attention", "relax", "attention", "relax"] if round_number % 2 else ["relax", "attention", "relax", "attention"]
                self.assertEqual([trial["condition"] for trial in plan], expected)
                self.assertEqual([trial["trial_order"] for trial in plan], [1, 2, 3, 4])
                self.assertEqual(len(plan), 4)
                # Explicit source indices ensure merging retains both original video pairs.
                attention_indices = (1, 2) if round_number == 1 else (3, 4)
                for position, trial in enumerate(plan):
                    condition = expected[position]
                    index = attention_indices[position // 2]
                    if condition == "attention":
                        index += (day - 1) * 4
                        attention_ids.append(trial["video"]["video_id"])
                    self.assertEqual(trial["video"]["path"], f"{condition}_video/{index:02d}.mp4")
                    self.assertEqual(trial["video"]["video_id"], f"{condition}_{index:02d}")
                    self.assertEqual(trial["video"]["filename"], f"{index:02d}.mp4")
                self.assertEqual(sum(trial["condition"] == "attention" for trial in plan), 2)
                self.assertEqual(sum(trial["condition"] == "relax" for trial in plan), 2)
        self.assertEqual(len(set(attention_ids)), 12)
        self.assertEqual(len(media_catalog(self.root)), 16)

    def test_manifest_file_order_does_not_change_trial_order_or_remove_repeated_conditions(self):
        self.edit("video_manifest.json", lambda data: data["trials"].reverse())
        plan = plan_for(self.root, 1, 2)
        self.assertEqual([trial["trial_order"] for trial in plan], [1, 2, 3, 4])
        self.assertEqual([trial["video"]["video_id"] for trial in plan],
                         ["relax_03", "attention_03", "relax_04", "attention_04"])

    def test_missing_media_names_the_exact_file(self):
        (self.root / "attention_video/12.mp4").unlink()
        with self.assertRaisesRegex(ConfigurationError, "12.mp4"):
            validate_manifest(self.root)

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
        with self.assertRaisesRegex(ConfigurationError, "24 个 trial"):
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
        self.edit("video_manifest.json", lambda data: data.update(schema_version="1.0"))
        with self.assertRaisesRegex(ConfigurationError, "schema_version=2.0"):
            validate_manifest(self.root)

    def test_wrong_source_video_pair_is_rejected(self):
        self.edit("video_manifest.json", lambda data: data["trials"][2]["video"].update(
            filename="01.mp4", video_id="attention_01", path="attention_video/01.mp4"))
        with self.assertRaisesRegex(ConfigurationError, "attention_video/02.mp4"):
            validate_manifest(self.root)

    def test_old_round_three_and_four_are_rejected_in_manifest(self):
        for round_number in (3, 4):
            with self.subTest(round=round_number):
                self.edit("video_manifest.json", lambda data: data["trials"][0].update(round=round_number))
                with self.assertRaisesRegex(ConfigurationError, "round 必须为整数 1 或 2"):
                    validate_manifest(self.root)

    def test_subject_is_safe_and_preserves_leading_zeroes(self):
        self.assertEqual(validate_subject(" 001 "), "001")
        self.assertEqual(validate_subject("被试_03"), "被试_03")
        for invalid in (None, 1, "", "  ", "../x", "..", "a/b", "a\\b", "a:b", "a*", "x.",
                        "CON", "nul.txt", "COM1", "lpt9.data", "COM¹", "a\x00b", "x" * 65):
            with self.subTest(subject=invalid):
                with self.assertRaises(ConfigurationError):
                    validate_subject(invalid)

    def test_day_and_round_cannot_be_coerced_from_unsafe_types(self):
        for day, round_number in ((0, 1), (4, 1), (1, 0), (1, 3), (1, 4), (1, 5), (True, 1), (1, False), ("1", 1), (1, 1.0)):
            with self.subTest(day=day, round=round_number):
                with self.assertRaises(ConfigurationError):
                    plan_for(self.root, day, round_number)

    def test_media_metadata_sha256_and_cache_invalidation(self):
        video = plan_for(self.root, 1, 1)[0]["video"]
        source = self.root / video["path"]
        self.assertEqual(video["duration_seconds"], 2)
        self.assertEqual(video["codecs"], ["avc1"])
        self.assertEqual(video["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(video["url"], "/media/attention_video/01.mp4")
        self.assertEqual(video["size_bytes"], source.stat().st_size)
        self.assertEqual(video["mtime_ns"], source.stat().st_mtime_ns)
        source.write_bytes(movie(23, version=1, codec=b"av01"))
        changed = plan_for(self.root, 1, 1)[0]["video"]
        self.assertEqual(changed["duration_seconds"], 23)
        self.assertEqual(changed["codecs"], ["av01"])
        self.assertNotEqual(changed["sha256"], video["sha256"])

    def test_corrupt_mp4_is_not_treated_as_playable_media(self):
        (self.root / "attention_video/01.mp4").write_bytes(b"not an mp4 file")
        with self.assertRaises(ConfigurationError):
            plan_for(self.root, 1, 1)


if __name__ == "__main__":
    unittest.main()
