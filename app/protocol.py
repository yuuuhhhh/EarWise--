"""Two-channel 33-byte stream framing copied from the verified reference layout.

Only the start byte and stop nibble are checked; this protocol has no verified
CRC. ADC values are returned unchanged as signed 24-bit counts.
"""

FRAME_SIZE = 33
START_BYTE = 0xA0
STOP_NIBBLE = 0xC0
ADC_MIN = -(1 << 23)
ADC_MAX = (1 << 23) - 1


def parse_24bit_signed(data: bytes) -> int:
    if len(data) != 3:
        raise ValueError("A signed 24-bit value must contain exactly three bytes")
    return int.from_bytes(data, "big", signed=True)


def encode_frame(sequence: int, channel_0: int, channel_1: int) -> bytes:
    """Construct a protocol frame for the explicitly labelled simulator/tests."""
    if not 0 <= sequence <= 255:
        raise ValueError("Device sequence must be in [0, 255]")
    if not (ADC_MIN <= channel_0 <= ADC_MAX and ADC_MIN <= channel_1 <= ADC_MAX):
        raise ValueError("Channel values must fit signed 24-bit ADC counts")
    frame = bytearray(FRAME_SIZE)
    frame[0] = START_BYTE
    frame[1] = sequence
    frame[2:5] = channel_0.to_bytes(3, "big", signed=True)
    frame[5:8] = channel_1.to_bytes(3, "big", signed=True)
    frame[-1] = STOP_NIBBLE
    return bytes(frame)


class FrameParser:
    """Reassemble split/coalesced BLE notifications without fabricating rows.

    error_count is the cumulative count of bytes discarded during framing
    recovery, not a claim about the number of corrupt EEG samples. reset()
    discards only an incomplete buffer and preserves that cumulative counter.
    """

    def __init__(self):
        self._buffer = bytearray()
        self.error_count = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self):
        self._buffer.clear()

    def feed(self, data: bytes) -> list[dict]:
        self._buffer.extend(data)
        frames = []
        while self._buffer:
            start = self._buffer.find(START_BYTE)
            if start < 0:
                self.error_count += len(self._buffer)
                self._buffer.clear()
                break
            if start:
                self.error_count += start
                del self._buffer[:start]
            if len(self._buffer) < FRAME_SIZE:
                break
            if self._buffer[FRAME_SIZE - 1] & 0xF0 != STOP_NIBBLE:
                self.error_count += 1
                del self._buffer[0]
                continue
            original = bytes(self._buffer[:FRAME_SIZE])
            del self._buffer[:FRAME_SIZE]
            frames.append({
                "device_seq": original[1],
                "channel_0_raw": parse_24bit_signed(original[2:5]),
                "channel_1_raw": parse_24bit_signed(original[5:8]),
                "original_frame_hex": original.hex(),
            })
        return frames
