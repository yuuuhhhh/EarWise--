import asyncio
import math
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.device import RealDevice, SimulatedDevice
from app.protocol import ADC_MAX, ADC_MIN, FrameParser, encode_frame, parse_24bit_signed
from app.quality import QualityTracker


def sample(sequence, left=0, right=0):
    return {"device_seq": sequence, "channel_0_raw": left, "channel_1_raw": right}


class ProtocolTests(unittest.TestCase):
    def test_signed_24_bit_boundaries_and_exact_two_channels(self):
        parser = FrameParser()
        for value in [ADC_MIN, -1, 0, 1, ADC_MAX]:
            packet = encode_frame(255, value, -1 if value == ADC_MIN else -value)
            frame, = parser.feed(packet)
            self.assertEqual(frame["channel_0_raw"], value)
            self.assertEqual(frame["device_seq"], 255)
            self.assertEqual(frame["original_frame_hex"], packet.hex())
            self.assertNotIn("channel_2_raw", frame)
        self.assertEqual(parse_24bit_signed(b"\x80\x00\x00"), ADC_MIN)
        with self.assertRaises(ValueError):
            parse_24bit_signed(b"\x00\x00")

    def test_every_split_and_coalesced_notifications(self):
        first = encode_frame(1, -100, 200)
        second = encode_frame(2, 300, -400)
        for split in range(1, 33):
            parser = FrameParser()
            self.assertEqual(parser.feed(first[:split]), [])
            frames = parser.feed(first[split:] + second)
            self.assertEqual([f["device_seq"] for f in frames], [1, 2])
            self.assertEqual(parser.error_count, 0)

    def test_garbage_bad_tail_and_resynchronization(self):
        parser = FrameParser()
        malformed = bytearray(encode_frame(5, 1, 2))
        malformed[-1] = 0x10
        good = bytearray(encode_frame(6, -4, 9))
        good[-1] = 0xCF
        frames = parser.feed(b"junk" + malformed + good)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["device_seq"], 6)
        self.assertEqual(parser.error_count, 37)

    def test_reset_prevents_joining_epochs_and_preserves_error_counter(self):
        parser = FrameParser()
        frame = encode_frame(10, 20, 30)
        parser.feed(b"noise" + frame[:10])
        self.assertEqual(parser.buffered_bytes, 10)
        parser.reset()
        self.assertEqual(parser.buffered_bytes, 0)
        self.assertEqual(parser.error_count, 5)
        self.assertEqual(parser.feed(frame[10:]), [])
        self.assertEqual(len(parser.feed(frame)), 1)


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.quality = QualityTracker({})
        self.quality.reset(0)

    def test_empty_does_not_report_zero_quality(self):
        snapshot = self.quality.snapshot(0)
        self.assertEqual(snapshot["data_status"], "waiting")
        self.assertIsNone(snapshot["loss_pct"])
        self.assertIsNone(snapshot["saturation_0_pct"])

    def test_normal_wrap_and_known_loss(self):
        for i, seq in enumerate([254, 255, 0, 1]):
            self.quality.ingest(sample(seq), i * 4_000_000)
        self.assertEqual(self.quality.snapshot(12_000_000)["loss_pct"], 0)
        result = self.quality.ingest(sample(4), 24_000_000)
        self.assertEqual(result["missing_frames"], 2)
        snapshot = self.quality.snapshot(24_000_000)
        self.assertEqual(snapshot["unique_frames"], 5)
        self.assertEqual(snapshot["lost_frames"], 2)
        self.assertAlmostEqual(snapshot["loss_pct"], 100 * 2 / 7)

    def test_duplicates_do_not_dilute_loss_or_saturation(self):
        self.quality.ingest(sample(10, ADC_MAX, 0), 0)
        self.quality.ingest(sample(13, 0, ADC_MIN), 12_000_000)
        for i in range(10):
            result = self.quality.ingest(sample(13, 0, ADC_MIN), 13_000_000 + i)
            self.assertEqual(result["frame_status"], "duplicate")
        snapshot = self.quality.snapshot(14_000_000)
        self.assertEqual(snapshot["duplicate_frames"], 10)
        self.assertEqual(snapshot["unique_frames"], 2)
        self.assertEqual(snapshot["loss_pct"], 50)
        self.assertEqual(snapshot["saturation_0_pct"], 50)
        self.assertEqual(snapshot["saturation_1_pct"], 50)

    def test_recent_nonconsecutive_duplicate_is_excluded(self):
        self.quality.ingest(sample(10, 1), 0)
        self.quality.ingest(sample(11, 2), 4_000_000)
        result = self.quality.ingest(sample(10, 1), 5_000_000)
        self.assertEqual(result["frame_status"], "duplicate")
        self.quality.ingest(sample(12, 3), 8_000_000)
        self.assertEqual(self.quality.totals["unique_frames"], 3)
        self.assertEqual(self.quality.totals["lost_frames"], 0)

    def test_reset_out_of_order_and_different_payload_are_unknown(self):
        for previous, current in [(100, 0), (20, 19), (42, 42)]:
            with self.subTest(previous=previous, current=current):
                self.quality.reset(0)
                self.quality.ingest(sample(previous, 1), 0)
                result = self.quality.ingest(sample(current, 2), 4_000_000)
                snapshot = self.quality.snapshot(4_000_000)
                self.assertEqual(result["frame_status"], "sequence_ambiguous")
                self.assertEqual(result["stream_epoch_id"], 2)
                self.assertEqual(result["missing_frames"], 0)
                self.assertTrue(snapshot["unknown_gap"])
                self.assertIsNone(snapshot["loss_pct"])

    def test_long_gap_delta_one_cannot_hide_full_wrap(self):
        self.quality.ingest(sample(10), 0)
        stale = self.quality.snapshot(1_100_000_000)
        self.assertEqual(stale["data_status"], "interrupted")
        self.assertIsNone(stale["saturation_0_pct"])
        self.assertIsNone(stale["loss_pct"])
        result = self.quality.ingest(sample(11), 2_000_000_000)
        self.assertEqual(result["frame_status"], "sequence_ambiguous")
        self.assertEqual(result["missing_frames"], 0)
        self.assertEqual(self.quality.totals["unknown_gaps"], 1)

    def test_sub_timeout_but_untrusted_gap_is_unknown(self):
        self.quality.ingest(sample(0), 0)
        result = self.quality.ingest(sample(1), 600_000_000)
        self.assertEqual(result["anomaly_reason"], "sequence_time_ambiguity")
        self.assertIsNone(self.quality.snapshot(600_000_000)["loss_pct"])

    def test_threshold_boundaries_independent_channels(self):
        upper = math.floor(ADC_MAX * 0.98)
        lower = math.ceil(ADC_MIN * 0.98)
        data = [(upper, 0), (lower, 0), (upper - 1, ADC_MAX), (lower + 1, 0)]
        for i, (left, right) in enumerate(data):
            self.quality.ingest(sample(i, left, right), i * 4_000_000)
        snapshot = self.quality.snapshot(12_000_000)
        self.assertEqual(snapshot["saturation_0_pct"], 50)
        self.assertEqual(snapshot["saturation_1_pct"], 25)
        self.assertAlmostEqual(snapshot["actual_window_seconds"], .012)
        self.assertTrue(snapshot["data_insufficient"])

    def test_window_ages_unknown_and_samples_but_totals_survive(self):
        self.quality.ingest(sample(100, ADC_MAX), 0)
        self.quality.ingest(sample(0), 4_000_000)
        for i in range(1, 1400):
            self.quality.ingest(sample(i % 256), 4_000_000 * (i + 1))
        snapshot = self.quality.snapshot(5_600_000_000)
        self.assertFalse(snapshot["unknown_gap"])
        self.assertEqual(snapshot["loss_pct"], 0)
        self.assertEqual(snapshot["saturation_0_pct"], 0)
        self.assertEqual(self.quality.totals["unknown_gaps"], 1)
        self.assertEqual(self.quality.totals["saturated_0_frames"], 1)

    def test_large_single_notification_repeated_values_survive_wrap(self):
        for i in range(600):
            self.quality.ingest(sample(i % 256), 0)
        self.assertEqual(self.quality.totals["unique_frames"], 600)
        self.assertEqual(self.quality.totals["duplicate_frames"], 0)

    def test_reset_excludes_preview_and_external_epoch_interrupts_quality(self):
        self.quality.ingest(sample(10, ADC_MAX), 0)
        self.quality.new_epoch("BLE disconnected", 4_000_000)
        self.assertEqual(self.quality.snapshot(5_000_000)["data_status"], "interrupted")
        self.quality.reset(10_000_000)
        self.assertEqual(self.quality.totals["received_frames"], 0)
        self.assertFalse(self.quality.totals["unknown_gap"])
        self.assertEqual(self.quality.snapshot(10_000_000)["unique_frames"], 0)
        self.quality.ingest(sample(200), 10_000_000)
        self.assertEqual(self.quality.totals["lost_frames"], 0)

    def test_invalid_raw_and_backwards_times_are_rejected(self):
        with self.assertRaises(ValueError):
            self.quality.ingest(sample(0, ADC_MAX + 1), 0)
        self.quality.ingest(sample(0), 100)
        with self.assertRaises(ValueError):
            self.quality.ingest(sample(1), 99)


class DeviceTests(unittest.IsolatedAsyncioTestCase):
    async def test_simulation_emits_parseable_frames_idempotently(self):
        notifications, statuses = [], []
        device = SimulatedDevice({}, notifications.append, lambda status, detail: statuses.append(status))
        await device.connect()
        task = device._task
        await device.connect()
        self.assertIs(device._task, task)
        await asyncio.sleep(.1)
        await device.disconnect()
        parser = FrameParser()
        frames = [frame for notification in notifications for frame in parser.feed(notification)]
        self.assertGreaterEqual(len(frames), 20)
        self.assertEqual([frame["device_seq"] for frame in frames], list(range(len(frames))))
        self.assertEqual(parser.error_count, 0)
        self.assertEqual(statuses, ["connected", "disconnected"])

    async def test_real_connect_does_not_send_commands_and_is_idempotent(self):
        client = SimpleNamespace(is_connected=True, connect=AsyncMock(), disconnect=AsyncMock(),
                                 start_notify=AsyncMock(), write_gatt_char=AsyncMock(),
                                 services=SimpleNamespace(get_service=lambda uuid: object()))
        scanner = SimpleNamespace(find_device_by_filter=AsyncMock(return_value=SimpleNamespace(name="MindBridge-v3.11")))
        statuses = []
        device = RealDevice({}, lambda data: None, lambda status, detail: statuses.append(status))
        with patch("bleak.BleakClient", return_value=client), patch("bleak.BleakScanner", scanner):
            await device.connect()
            await device.connect()
            client.connect.assert_awaited_once()
            client.start_notify.assert_awaited_once()
            client.write_gatt_char.assert_not_awaited()
            await device.command("b")
            self.assertEqual(client.write_gatt_char.await_args.args[1], b"b")
            with self.assertRaises(ValueError):
                await device.command("B")
            await device.disconnect()
            client.disconnect.assert_awaited_once()
        self.assertEqual(statuses, ["searching", "connecting", "connected", "disconnected"])


if __name__ == "__main__":
    unittest.main()
