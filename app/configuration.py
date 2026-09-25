"""Validated experiment configuration and read-only metadata for the original media."""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import uuid
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote


EXPERIMENT_PROTOCOL_ID = "earwise-3day-2round-4trial-v2"
TRIALS_PER_ROUND = 4
ROUNDS_PER_DAY = 2


class ConfigurationError(ValueError):
    """An actionable project configuration or media validation failure."""


def _json(path: Path):
    try:
        with path.open("r", encoding="utf-8-sig") as source:
            return json.load(source)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"无法读取配置 {path}：{exc}") from exc


def _number(settings, key, minimum, maximum, *, integer=False):
    value = settings.get(key)
    types = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, types) or not math.isfinite(value):
        raise ConfigurationError(f"settings.json：{key} 必须为{'整数' if integer else '有限数值'}")
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"settings.json：{key} 必须在 {minimum}–{maximum} 之间")


def load_settings(root: Path) -> dict:
    settings = _json(Path(root) / "config" / "settings.json")
    if not isinstance(settings, dict):
        raise ConfigurationError("settings.json 顶层必须为对象")
    if settings.get("schema_version") != "1.0":
        raise ConfigurationError("settings.json：schema_version 必须为 1.0")
    if not isinstance(settings.get("software_version"), str) or not settings["software_version"].strip():
        raise ConfigurationError("settings.json：缺少 software_version")
    _number(settings, "baseline_seconds", 30, 30, integer=True)
    for key, minimum, maximum, integer in (
        ("nominal_sample_rate", 1, 10000, True),
        ("window_seconds", 0.1, 120, False),
        ("data_timeout_seconds", 0.1, 60, False),
        ("sequence_max_delta", 1, 127, True),
        ("sequence_trust_seconds", 0.001, 60, False),
        ("saturation_ratio", 0.01, 1, False),
        ("page_heartbeat_seconds", 1, 120, False),
        ("video_timeout_seconds", 1, 120, False),
        ("queue_max_items", 250, 1000000, True),
        ("queue_max_age_seconds", 0.1, 60, False),
        ("uv_scale", 0.000000001, 1000000, False),
    ):
        _number(settings, key, minimum, maximum, integer=integer)
    if settings["sequence_trust_seconds"] >= 256 / settings["nominal_sample_rate"]:
        raise ConfigurationError("sequence_trust_seconds 必须短于名义采样率下的 8 位序号回绕周期")
    for key in ("baseline_instruction", "target_name"):
        if not isinstance(settings.get(key), str) or not settings[key].strip():
            raise ConfigurationError(f"settings.json：{key} 必须为非空字符串")
    for key in ("service_uuid", "notify_uuid", "write_uuid"):
        try:
            uuid.UUID(settings[key])
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ConfigurationError(f"settings.json：{key} 不是有效 UUID") from exc
    if type(settings.get("uv_verified")) is not bool:
        raise ConfigurationError("settings.json：uv_verified 必须为 true/false")
    mapping = settings.get("channel_mapping")
    if not isinstance(mapping, dict) or type(mapping.get("verified")) is not bool:
        raise ConfigurationError("settings.json：channel_mapping 缺少布尔 verified")
    if any(key not in mapping or mapping[key] not in (None, "left", "right") for key in ("channel_0", "channel_1")):
        raise ConfigurationError("channel_mapping 通道值只能为 left、right 或 null")
    if mapping["verified"] and {mapping.get("channel_0"), mapping.get("channel_1")} != {"left", "right"}:
        raise ConfigurationError("已验证的 channel_mapping 必须恰好包含一个 left 和一个 right")
    validations = settings.get("validations")
    if not isinstance(validations, dict):
        raise ConfigurationError("settings.json：缺少 validations 对象")
    for key in ("commands_verified", "sample_rate_verified", "saturation_verified"):
        if type(validations.get(key)) is not bool:
            raise ConfigurationError(f"validations.{key} 必须为 true/false")
    commands = settings.get("commands")
    if not isinstance(commands, list) or not commands or any(command not in ("S", "b", "R", "I") for command in commands):
        raise ConfigurationError("commands 必须为非空命令列表；支持参考脚本的 S、b、R、I，保留大小写")
    questionnaires = settings.get("questionnaires")
    if not isinstance(questionnaires, dict):
        raise ConfigurationError("settings.json：缺少 questionnaires 对象")
    for condition in ("attention", "relax"):
        questions = questionnaires.get(condition)
        if not isinstance(questions, dict):
            raise ConfigurationError(f"questionnaires 缺少 {condition}")
        for key in ("question", "confidence_question", "low_label", "high_label"):
            if not isinstance(questions.get(key), str) or not questions[key].strip():
                raise ConfigurationError(f"questionnaires.{condition}.{key} 必须为非空字符串")
    _number(settings, "adc_bits", 24, 24, integer=True)
    _number(settings, "candidate_adc_gain", 0.001, 1000000)
    _number(settings, "quality_refresh_seconds", 1, 1)
    if settings.get("uv_unit") != "uV":
        raise ConfigurationError("settings.json：当前换算输出字段仅支持 uv_unit=uV")
    expected_protocol = {"frame_bytes": 33, "header_hex": "A0", "tail_high_nibble_hex": "C0",
                         "sequence_offset": 1, "sequence_bits": 8, "raw_offsets": [[2, 5], [5, 8]],
                         "endianness": "big", "signed": True, "crc_verified": False}
    protocol = settings.get("protocol")
    if not isinstance(protocol, dict) or any(
        type(protocol.get(key)) is not type(value) or protocol[key] != value
        for key, value in expected_protocol.items()
    ):
        raise ConfigurationError("protocol 与当前 33 字节双通道解析器不匹配；不能只改配置宣称新协议或 CRC 验证")
    algorithms = settings.get("algorithm_versions")
    if not isinstance(algorithms, dict) or algorithms.get("saturation") != "raw-adc-threshold-v1" or algorithms.get("packet_loss") != "seq8-conservative-window-v1":
        raise ConfigurationError("algorithm_versions 必须与当前实现 raw-adc-threshold-v1 / seq8-conservative-window-v1 一致")
    return settings


_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.IGNORECASE)


def validate_subject(subject_id: str) -> str:
    if not isinstance(subject_id, str):
        raise ConfigurationError("被试编号必须为字符串")
    subject_id = subject_id.strip()
    if not subject_id or len(subject_id) > 64:
        raise ConfigurationError("被试编号必须为 1–64 个字符")
    if any(ord(char) < 32 or ord(char) == 127 for char in subject_id) or any(
        char in '<>:"/\\|?*' for char in subject_id
    ) or ".." in subject_id or subject_id.endswith((".", " ")):
        raise ConfigurationError("被试编号不能包含路径分隔符、连续点、控制字符或 Windows 文件名禁用字符")
    if _RESERVED.match(subject_id):
        raise ConfigurationError("被试编号不能使用 Windows 保留名称（如 CON、NUL、COM1）")
    return subject_id


def _day_round(day, round):
    if type(day) is not int or day not in (1, 2, 3):
        raise ConfigurationError("day 必须为整数 1、2 或 3")
    if type(round) is not int or round not in range(1, ROUNDS_PER_DAY + 1):
        raise ConfigurationError("round 必须为整数 1 或 2")


def _media_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path.parent.name not in ("attention_video", "relax_video"):
        raise ConfigurationError(f"视频路径超出指定素材目录：{relative}")
    if not path.is_file():
        raise ConfigurationError(f"视频文件不存在：{path}")
    try:
        with path.open("rb") as source:
            if not source.read(1):
                raise ConfigurationError(f"视频文件为空：{path}")
    except OSError as exc:
        raise ConfigurationError(f"视频文件不可读：{path}：{exc}") from exc
    return path


def validate_manifest(root: Path) -> list[dict]:
    root = Path(root)
    manifest = _json(root / "config" / "video_manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "2.0":
        raise ConfigurationError("video_manifest.json 必须为 schema_version=2.0 的对象")
    entries = manifest.get("trials")
    if not isinstance(entries, list) or len(entries) != 24:
        raise ConfigurationError("视频清单必须包含 24 个 trial，覆盖全部 6 个 day × round，每 round 4 个 trial，每 trial 一个视频")
    validated, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigurationError("视频清单 trial 必须为对象")
        day, round, condition = entry.get("day"), entry.get("round"), entry.get("condition")
        trial_order = entry.get("trial_order")
        _day_round(day, round)
        if type(trial_order) is not int or trial_order not in range(1, TRIALS_PER_ROUND + 1):
            raise ConfigurationError(f"day {day} round {round} 的 trial_order 必须为整数 1、2、3 或 4")
        key = (day, round, trial_order)
        if key in seen:
            raise ConfigurationError(f"视频清单重复：day {day} round {round} trial_order {trial_order}")
        seen.add(key)
        expected_condition = "attention" if (round + trial_order) % 2 == 0 else "relax"
        if condition != expected_condition:
            raise ConfigurationError(f"day {day} round {round} trial_order {trial_order} 的 condition 必须为 {expected_condition}")
        pair_index = (round - 1) * 2 + (trial_order - 1) // 2 + 1
        index = (day - 1) * 4 + pair_index if condition == "attention" else pair_index
        relative = f"{condition}_video/{index:02d}.mp4"
        video_id = f"{condition}_{index:02d}"
        video = entry.get("video")
        if not isinstance(video, dict) or video.get("path") != relative or video.get("video_id") != video_id:
            raise ConfigurationError(f"day {day} round {round} {condition} 必须对应 {relative}（ID {video_id}），每 trial 恰好一个视频")
        if video.get("filename") != f"{index:02d}.mp4":
            raise ConfigurationError(f"视频 {video_id} 的 filename 必须为 {index:02d}.mp4")
        _media_path(root, relative)
        validated.append({"day": day, "round": round, "trial_order": trial_order, "condition": condition,
                          "video": {"video_id": video_id, "path": relative,
                                    "filename": video["filename"], "url": "/media/" + quote(relative)}})
    expected = {(day, round, trial_order) for day in (1, 2, 3)
                for round in range(1, ROUNDS_PER_DAY + 1)
                for trial_order in range(1, TRIALS_PER_ROUND + 1)}
    if seen != expected:
        raise ConfigurationError("视频清单必须完整覆盖 3 天、每天 2 个 round、每 round 4 个 trial")
    return validated


def _atoms(source: BinaryIO, start: int, end: int):
    position = start
    while position < end:
        if end - position < 8:
            raise ConfigurationError("MP4 box 尾部不完整")
        source.seek(position)
        raw = source.read(8)
        if len(raw) != 8:
            raise ConfigurationError("MP4 box 头不完整")
        length, kind = struct.unpack(">I4s", raw)
        header = 8
        if length == 1:
            extra = source.read(8)
            if len(extra) != 8:
                raise ConfigurationError("MP4 扩展 box 头不完整")
            length = struct.unpack(">Q", extra)[0]
            header = 16
        elif length == 0:
            length = end - position
        if length < header or position + length > end:
            raise ConfigurationError("MP4 box 长度无效")
        yield kind, position + header, position + length
        position += length


@lru_cache(maxsize=64)
def _metadata_cached(path_string: str, size: int, mtime_ns: int, ctime_ns: int) -> tuple:
    path = Path(path_string)
    duration = None
    codecs = set()

    def inspect(source, start, end, depth=0):
        nonlocal duration
        if depth > 8:
            raise ConfigurationError("MP4 嵌套层级异常")
        for kind, payload, stop in _atoms(source, start, end):
            if kind == b"mvhd":
                source.seek(payload)
                raw = source.read(min(stop - payload, 40))
                if len(raw) < 20:
                    raise ConfigurationError("MP4 mvhd 不完整")
                if raw[0] == 0:
                    timescale, units = struct.unpack(">II", raw[12:20])
                    invalid_units = 0xffffffff
                elif raw[0] == 1 and len(raw) >= 32:
                    timescale, units = struct.unpack(">IQ", raw[20:32])
                    invalid_units = 0xffffffffffffffff
                else:
                    raise ConfigurationError("MP4 mvhd 版本不支持")
                if timescale and units and units != invalid_units:
                    duration = units / timescale
            elif kind in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                inspect(source, payload, stop, depth + 1)
            elif kind == b"stsd":
                if stop - payload < 8:
                    raise ConfigurationError("MP4 stsd 不完整")
                for codec, _, _ in _atoms(source, payload + 8, stop):
                    codecs.add(codec.decode("ascii", errors="replace"))

    try:
        with path.open("rb") as source:
            inspect(source, 0, size)
    except OSError as exc:
        raise ConfigurationError(f"无法读取视频元数据 {path}：{exc}") from exc
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise ConfigurationError(f"无法读取有效 MP4 播放时长：{path}")
    return round(duration, 6), tuple(sorted(codecs))


@lru_cache(maxsize=64)
def _sha256_cached(path_string: str, size: int, mtime_ns: int, ctime_ns: int) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path_string).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ConfigurationError(f"无法计算视频 SHA-256 {path_string}：{exc}") from exc
    return digest.hexdigest()


def _video_metadata(root: Path, video: dict, *, with_hash: bool) -> dict:
    path = _media_path(root, video["path"])
    stat = path.stat()
    identity = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    duration, codecs = _metadata_cached(*identity)
    result = dict(video, duration_seconds=duration, codecs=list(codecs), size_bytes=stat.st_size,
                  mtime_ns=stat.st_mtime_ns, ctime_ns=stat.st_ctime_ns)
    if with_hash:
        result["sha256"] = _sha256_cached(*identity)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != identity[1:]:
        raise ConfigurationError(f"预检时视频文件发生变化，请重新预检：{path}")
    return result


def plan_for(root: Path, day: int, round: int) -> list[dict]:
    _day_round(day, round)
    matched = [entry for entry in validate_manifest(root)
               if entry["day"] == day and entry["round"] == round]
    return [{"condition": entry["condition"], "trial_order": entry["trial_order"],
             "video": _video_metadata(Path(root), entry["video"], with_hash=True)}
            for entry in sorted(matched, key=lambda trial: trial["trial_order"])]


def media_catalog(root: Path) -> list[dict]:
    videos = {entry["video"]["video_id"]: entry["video"] for entry in validate_manifest(root)}
    return [_video_metadata(Path(root), video, with_hash=False) for _, video in sorted(videos.items())]
