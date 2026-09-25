"""Auditable, bounded-window saturation and conservative sequence loss estimates.

An eight-bit sequence cannot prove continuity. A short gap and a small forward
delta are necessary assumptions, not hardware validation. Ambiguous intervals
are reported as unknown, never converted to precise loss counts.
"""

from collections import deque
import math
import time

from .protocol import ADC_MIN, ADC_MAX


ALGORITHM_VERSION = "seq8-conservative-window-v1"


class QualityTracker:
    def __init__(self, config: dict):
        self.config = dict(config)
        self.window_seconds = float(config.get("window_seconds", 5))
        self.timeout_seconds = float(config.get("data_timeout_seconds", 1))
        self.trust_seconds = float(config.get("sequence_trust_seconds", 0.5))
        self.max_delta = int(config.get("sequence_max_delta", 32))
        self.nominal_sample_rate = float(config.get("nominal_sample_rate", 250))
        self.saturation_ratio = float(config.get("saturation_ratio", 0.98))
        if self.window_seconds <= 0 or self.timeout_seconds <= 0 or self.trust_seconds <= 0:
            raise ValueError("Quality window and continuity time limits must be positive")
        if not 1 <= self.max_delta < 128:
            raise ValueError("sequence_max_delta must be between 1 and 127")
        if not 0 < self.saturation_ratio <= 1 or self.nominal_sample_rate <= 0:
            raise ValueError("Saturation ratio and sample rate are invalid")
        # Trust interval must remain shorter than one nominal sequence wrap.
        if self.trust_seconds >= 256 / self.nominal_sample_rate:
            raise ValueError("sequence_trust_seconds must be shorter than a sequence wrap")
        self.lower_threshold = math.ceil(ADC_MIN * self.saturation_ratio)
        self.upper_threshold = math.floor(ADC_MAX * self.saturation_ratio)
        self.reset()

    def reset(self, now_ns: int | None = None):
        self.stream_epoch_id = 1
        self._window = deque()
        self._unknown = deque()
        self._recent = deque()
        self._last_seq = None
        self._last_unique_ns = None
        self._last_frame_ns = None
        self._first_frame_ns = None
        self._reset_ns = time.monotonic_ns() if now_ns is None else now_ns
        self._forced_interrupted = False
        self._totals = {
            "received_frames": 0, "unique_frames": 0, "duplicate_frames": 0,
            "lost_frames": 0, "unknown_gaps": 0, "sequence_ambiguous_frames": 0,
            "saturated_0_frames": 0, "saturated_1_frames": 0,
        }

    @property
    def last_frame_monotonic_ns(self):
        return self._last_frame_ns

    @property
    def totals(self) -> dict:
        return {**self._totals, "unknown_gap": self._totals["unknown_gaps"] > 0,
                "stream_epoch_id": self.stream_epoch_id, "algorithm_version": ALGORITHM_VERSION}

    def new_epoch(self, reason: str, now_ns: int):
        """Reset continuity only; retain recent-window evidence and total counts.

        The controller must also reset its FrameParser at this boundary.
        """
        self.stream_epoch_id += 1
        self._last_seq = None
        self._last_unique_ns = None
        self._recent.clear()
        self._unknown.append((now_ns, str(reason)))
        self._totals["unknown_gaps"] += 1
        self._forced_interrupted = True
        self._trim(now_ns)

    def _trim(self, now_ns: int):
        cutoff = now_ns - int(self.window_seconds * 1e9)
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()
        while self._unknown and self._unknown[0][0] < cutoff:
            self._unknown.popleft()

    @staticmethod
    def _fingerprint(frame):
        return (frame["device_seq"], frame["channel_0_raw"], frame["channel_1_raw"],
                frame.get("original_frame_hex"))

    def ingest(self, frame: dict, now_ns: int) -> dict:
        seq = frame["device_seq"]
        if not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq <= 255:
            raise ValueError("Invalid device sequence")
        for channel in ("channel_0_raw", "channel_1_raw"):
            if (not isinstance(frame[channel], int) or isinstance(frame[channel], bool)
                    or not ADC_MIN <= frame[channel] <= ADC_MAX):
                raise ValueError("Invalid raw ADC count")
        if self._last_frame_ns is not None and now_ns < self._last_frame_ns:
            raise ValueError("Receive monotonic times cannot move backwards")
        self._totals["received_frames"] += 1
        fingerprint = self._fingerprint(frame)
        status = "normal"
        missing = 0
        reason = None
        if self._first_frame_ns is None:
            self._first_frame_ns = now_ns
        if self._last_seq is not None:
            elapsed = (now_ns - self._last_unique_ns) / 1e9
            delta = (seq - self._last_seq) & 0xFF
            while self._recent and now_ns - self._recent[0][0] > self.trust_seconds * 1e9:
                self._recent.popleft()
            if elapsed > self.trust_seconds:
                reason = "no_valid_data_timeout" if elapsed > self.timeout_seconds else "sequence_time_ambiguity"
            elif delta != 1 and any(item[1] == fingerprint for item in self._recent):
                status = "duplicate"
            elif 1 <= delta <= self.max_delta:
                missing = delta - 1
            else:
                reason = "sequence_reset_or_out_of_order" if delta else "same_sequence_different_payload"
        if reason:
            self.new_epoch(reason, now_ns)
            status = "sequence_ambiguous"
            self._totals["sequence_ambiguous_frames"] += 1
        self._last_frame_ns = now_ns
        self._forced_interrupted = False
        if status == "duplicate":
            self._totals["duplicate_frames"] += 1
            self._window.append((now_ns, 0, 1, 0, 0, 0))
        else:
            sat_0 = int(frame["channel_0_raw"] <= self.lower_threshold or frame["channel_0_raw"] >= self.upper_threshold)
            sat_1 = int(frame["channel_1_raw"] <= self.lower_threshold or frame["channel_1_raw"] >= self.upper_threshold)
            self._totals["unique_frames"] += 1
            self._totals["lost_frames"] += missing
            self._totals["saturated_0_frames"] += sat_0
            self._totals["saturated_1_frames"] += sat_1
            self._window.append((now_ns, 1, 0, missing, sat_0, sat_1))
            self._last_seq = seq
            self._last_unique_ns = now_ns
            self._recent.append((now_ns, fingerprint))
            # Keep less than half a sequence wrap, even for large BLE batches.
            while len(self._recent) > 127:
                self._recent.popleft()
        self._trim(now_ns)
        result = {"frame_status": status, "stream_epoch_id": self.stream_epoch_id,
                  "missing_frames": missing}
        if reason:
            result["anomaly_reason"] = reason
        return result

    def snapshot(self, now_ns: int) -> dict:
        self._trim(now_ns)
        age = None if self._last_frame_ns is None else max(0.0, (now_ns - self._last_frame_ns) / 1e9)
        if self._forced_interrupted or (age is not None and age > self.timeout_seconds):
            data_status = "interrupted"
        elif age is None:
            data_status = "waiting"
        else:
            data_status = "receiving"
        unique = sum(item[1] for item in self._window)
        duplicates = sum(item[2] for item in self._window)
        lost = sum(item[3] for item in self._window)
        sat_0 = sum(item[4] for item in self._window)
        sat_1 = sum(item[5] for item in self._window)
        unknown = bool(self._unknown) or data_status == "interrupted"
        fresh = data_status == "receiving" and unique > 0
        start = max(now_ns - int(self.window_seconds * 1e9), self._first_frame_ns or now_ns)
        # A legitimate synthetic/test monotonic timestamp can be zero.
        if self._first_frame_ns == 0:
            start = max(now_ns - int(self.window_seconds * 1e9), 0)
        actual_seconds = max(0.0, (now_ns - start) / 1e9)
        return {
            "saturation_0_pct": 100 * sat_0 / unique if fresh else None,
            "saturation_1_pct": 100 * sat_1 / unique if fresh else None,
            "loss_pct": 100 * lost / (unique + lost) if fresh and not unknown else None,
            "unique_frames": unique, "duplicate_frames": duplicates, "lost_frames": lost,
            "saturated_0_frames": sat_0, "saturated_1_frames": sat_1,
            "saturation_denominator": unique, "loss_denominator": unique + lost,
            "unknown_gap": unknown, "unknown_gap_count": len(self._unknown),
            "unknown_gap_reasons": list(dict.fromkeys(item[1] for item in self._unknown)),
            "window_seconds": self.window_seconds, "actual_window_seconds": actual_seconds,
            "window_start_monotonic_ns": start, "window_end_monotonic_ns": now_ns,
            "algorithm_version": ALGORITHM_VERSION, "data_status": data_status,
            "data_insufficient": not fresh or actual_seconds < self.window_seconds,
            "last_frame_monotonic_ns": self._last_frame_ns, "last_received_age_seconds": age,
            "stream_epoch_id": self.stream_epoch_id,
            "saturation_lower_threshold": self.lower_threshold,
            "saturation_upper_threshold": self.upper_threshold,
        }
