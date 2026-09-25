"""Bounded, dedicated disk writer. Session directories contain exactly three files."""
from __future__ import annotations

import csv
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable

from app.configuration import EXPERIMENT_PROTOCOL_ID, TRIALS_PER_ROUND, plan_fingerprint, randomization_for

EEG_FIELDS = ['session_id', 'sample_row_index', 'stream_epoch_id', 'notification_id',
              'frame_in_notification', 'device_seq', 'received_at_utc',
              'received_monotonic_ns', 'elapsed_ms', 'channel_0_raw', 'channel_1_raw',
              'channel_0_uv', 'channel_1_uv', 'frame_status', 'original_frame_hex']
LABEL_FIELDS = ['session_id', 'event_id', 'event_type', 'event_time_utc',
                'event_monotonic_ns', 'elapsed_ms', 'server_received_monotonic_ns',
                'client_event_id', 'client_performance_ms', 'sync_uncertainty_ms',
                'last_sample_row_index', 'stream_epoch_id', 'stage', 'trial_id',
                'trial_order', 'condition', 'video_id', 'video_order_in_trial',
                'media_position_ms', 'attention_score', 'relax_score',
                'confidence_score', 'details_json']


def atomic_json(path: Path, value: dict):
    temp = path.with_suffix('.json.tmp')
    with temp.open('w', encoding='utf-8', newline='\n') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


class Recorder:
    def __init__(self, directory: Path, snapshot: dict, on_error: Callable,
                 max_items: int = 20000, max_age_seconds: float = 2):
        self.directory = directory
        self.snapshot = snapshot
        self.on_error = on_error
        self.queue = queue.Queue(maxsize=max_items)
        self.max_age_seconds = max_age_seconds
        self.error = None
        self.closed = False
        self.row_count = 0
        self.event_count = 0
        self.events = []
        self.pending_since = None
        self._thread = None
        self._handles = []

    def initialize(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        try:
            atomic_json(self.directory / 'config.json', self.snapshot)
            self._eeg = (self.directory / 'eeg_raw.csv').open('x', encoding='utf-8', newline='')
            self._handles.append(self._eeg)
            self._labels = (self.directory / 'labels.csv').open('x', encoding='utf-8', newline='')
            self._handles.append(self._labels)
            self._ew = csv.DictWriter(self._eeg, fieldnames=EEG_FIELDS, extrasaction='ignore')
            self._lw = csv.DictWriter(self._labels, fieldnames=LABEL_FIELDS, extrasaction='ignore')
            self._ew.writeheader()
            self._lw.writeheader()
            self._flush()
            self._thread = threading.Thread(target=self._run, name='eeg-disk-writer', daemon=True)
            self._thread.start()
        except Exception as exc:
            for f in self._handles:
                f.close()
            self.snapshot.update(session_status='save_failed', termination_reason=str(exc))
            try:
                atomic_json(self.directory / 'config.json', self.snapshot)
            except OSError:
                pass
            raise

    def _fail(self, exc):
        if self.error is None:
            self.error = str(exc)
            self.on_error(self.error)

    def submit(self, kind: str, row: dict | None = None):
        if self.closed or self.error:
            raise OSError(self.error or '记录器已经关闭')
        try:
            self.queue.put_nowait((time.monotonic(), kind, row))
        except queue.Full as exc:
            self._fail('写入队列已满，已停止本轮采集')
            raise OSError(self.error) from exc

    def _flush(self):
        for f in self._handles:
            f.flush()
            os.fsync(f.fileno())

    def _run(self):
        last_flush = time.monotonic()
        try:
            while True:
                try:
                    queued_at, kind, row = self.queue.get(timeout=0.2)
                except queue.Empty:
                    if time.monotonic() - last_flush >= 1:
                        self._flush()
                        last_flush = time.monotonic()
                    continue
                if time.monotonic() - queued_at > self.max_age_seconds:
                    self._fail('写入队列持续积压，数据保存未能跟上采集')
                if kind == 'close':
                    break
                if kind == 'eeg':
                    self._ew.writerow(row)
                    self.row_count += 1
                elif kind == 'event':
                    self._lw.writerow(row)
                    self.events.append((row['event_type'], row.get('trial_id')))
                    self.event_count += 1
                elif kind == 'barrier':
                    self._flush()
                    row['signal'].set()
                elif kind == 'snapshot':
                    atomic_json(self.directory / 'config.json', row)
                if kind == 'flush' or time.monotonic() - last_flush >= 1:
                    self._flush()
                    last_flush = time.monotonic()
            self._flush()
        except Exception as exc:
            self._fail(exc)
        finally:
            for f in self._handles:
                try:
                    f.close()
                except Exception as exc:
                    self._fail(exc)

    def barrier(self):
        signal = threading.Event()
        self.submit('barrier', {'signal': signal})
        while not signal.wait(0.1):
            if self.error or not self._thread.is_alive():
                raise OSError(self.error or '写入线程意外退出')

    def finish(self, snapshot: dict, expect_completed: bool):
        self.closed = True
        if self._thread and self._thread.is_alive():
            while self._thread.is_alive():
                try:
                    self.queue.put((time.monotonic(), 'close', None), timeout=0.2)
                    break
                except queue.Full:
                    continue
            self._thread.join()
        if self.error:
            snapshot['session_status'] = 'save_failed'
            snapshot['termination_reason'] = self.error
        try:
            self.verify(snapshot, expect_completed and not self.error)
        except Exception as exc:
            snapshot['session_status'] = 'save_failed'
            snapshot['termination_reason'] = str(exc)
            self.error = str(exc)
        snapshot['summary']['actual_rows'] = self.row_count
        snapshot['summary']['event_rows'] = self.event_count
        atomic_json(self.directory / 'config.json', snapshot)
        return snapshot

    def verify(self, snapshot: dict, complete: bool):
        expected = {'eeg_raw.csv', 'labels.csv', 'config.json'}
        if {p.name for p in self.directory.iterdir()} != expected:
            raise OSError('会话文件数量校验失败')
        rows = 0
        with (self.directory / 'eeg_raw.csv').open(encoding='utf-8', newline='') as f:
            for row in csv.DictReader(f):
                if row['session_id'] != snapshot['session_id'] or int(row['sample_row_index']) != rows:
                    raise OSError('原始数据身份或行号校验失败')
                rows += 1
        if rows != self.row_count:
            raise OSError('原始数据行数校验失败')
        events = []
        persisted_labels = []
        with (self.directory / 'labels.csv').open(encoding='utf-8', newline='') as f:
            for row in csv.DictReader(f):
                if row['session_id'] != snapshot['session_id']:
                    raise OSError('标签身份校验失败')
                events.append((row['event_type'], row['trial_id']))
                persisted_labels.append(row)
        if len(events) != self.event_count:
            raise OSError('标签行数校验失败')
        if complete:
            trials = snapshot.get('trials', [])
            if snapshot.get('experiment_protocol_id') != EXPERIMENT_PROTOCOL_ID or len(trials) != TRIALS_PER_ROUND:
                raise OSError('实验协议或 trial 数量校验失败：每轮必须完成 4 个 trial')
            trial_ids = [trial.get('trial_id') for trial in trials]
            video_ids = [trial.get('video', {}).get('video_id') for trial in trials]
            if (any(not isinstance(value, str) or not value for value in trial_ids + video_ids)
                    or len(set(trial_ids)) != TRIALS_PER_ROUND or len(set(video_ids)) != TRIALS_PER_ROUND):
                raise OSError('trial 身份或视频身份校验失败：每个 trial 和视频必须唯一')
            rnd, day = snapshot.get('round'), snapshot.get('day')
            if type(rnd) is not int or rnd != 1:
                raise OSError('round 编号校验失败')
            if type(day) is not int or day not in (1, 2, 3):
                raise OSError('day 编号校验失败')
            randomization = randomization_for(snapshot.get('subject_id'), day)
            if snapshot.get('randomization') != randomization:
                raise OSError('随机顺序元数据校验失败')
            first = randomization['first_condition']
            conditions = (first, 'relax' if first == 'attention' else 'attention')
            for index, trial in enumerate(trials, 1):
                if type(trial.get('trial_order')) is not int or trial['trial_order'] != index or trial.get('condition') != conditions[(index - 1) % 2]:
                    raise OSError('trial 顺序或交替条件校验失败')
                condition = trial['condition']
                video_index = (index - 1) // 2 + 1
                if condition == 'attention':
                    video_index += (day - 1) * 2
                video = trial['video']
                if (video.get('video_id') != f'{condition}_{video_index:02d}'
                        or video.get('path') != f'{condition}_video/{video_index:02d}.mp4'
                        or video.get('filename') != f'{video_index:02d}.mp4'):
                    raise OSError('当天视频素材或同条件视频顺序校验失败')
            plan = [{key: trial[key] for key in ('condition', 'trial_order', 'video')} for trial in trials]
            if snapshot.get('plan_id') != plan_fingerprint(snapshot['subject_id'], day, plan):
                raise OSError('播放计划指纹校验失败')
            types = [e[0] for e in events]
            required = {'SESSION_START': 1, 'SESSION_END': 1, 'BASELINE_START': 1,
                        'BASELINE_END': 1, 'TRIAL_START': TRIALS_PER_ROUND,
                        'TRIAL_END': TRIALS_PER_ROUND, 'RATING_SUBMITTED': TRIALS_PER_ROUND}
            if not rows or any(types.count(k) != count for k, count in required.items()):
                raise OSError('完整性校验失败：缺少样本、阶段或问卷')
            expected_order = ['SESSION_START', 'BASELINE_START', 'BASELINE_END']
            expected_order += ['TRIAL_START', 'TRIAL_END', 'RATING_SUBMITTED'] * TRIALS_PER_ROUND
            expected_order.append('SESSION_END')
            if [kind for kind in types if kind in required] != expected_order:
                raise OSError('阶段边界或问卷提交顺序校验失败')
            for trial in trials:
                for kind in ('TRIAL_START', 'TRIAL_END', 'RATING_SUBMITTED'):
                    if events.count((kind, trial['trial_id'])) != 1:
                        raise OSError('trial 边界或问卷关联校验失败')
                    row = next(row for row in persisted_labels if row['event_type'] == kind and row['trial_id'] == trial['trial_id'])
                    if (row['trial_order'] != str(trial['trial_order']) or row['condition'] != trial['condition']
                            or row['video_id'] != trial['video']['video_id'] or row['video_order_in_trial'] != '1'):
                        raise OSError('trial 顺序、条件或视频关联校验失败')
                rating = next(row for row in persisted_labels if row['event_type'] == 'RATING_SUBMITTED' and row['trial_id'] == trial['trial_id'])
                condition = trial['condition']
                other = 'relax' if condition == 'attention' else 'attention'
                if rating['condition'] != condition or rating[f'{other}_score'] != '':
                    raise OSError('量表条件或不适用评分字段校验失败')
                if rating[f'{condition}_score'] not in ('1','2','3','4','5') or rating['confidence_score'] not in ('1','2','3','4','5'):
                    raise OSError('持久化量表评分校验失败')
            for kind in ('TRIAL_START', 'TRIAL_END', 'RATING_SUBMITTED'):
                if [trial_id for event_type, trial_id in events if event_type == kind] != trial_ids:
                    raise OSError('trial 执行顺序校验失败')


def recover_sessions(data_root: Path):
    """Never relabel terminal sessions; recover only process-orphaned recordings."""
    recovered = []
    if not data_root.exists():
        return recovered
    from datetime import datetime, timezone
    for path in data_root.glob('sub_*/day_*/round_*/*/config.json'):
        try:
            snapshot = json.loads(path.read_text(encoding='utf-8'))
            if snapshot.get('session_status') != 'recording':
                continue
            reason = '上次进程在记录期间退出；原始尾部可能未持久化'
            snapshot.update(session_status='interrupted', termination_reason=reason,
                            ended_at_utc=datetime.now(timezone.utc).isoformat())
            # Recovery cannot fabricate a previous-process monotonic timestamp.
            label = path.parent / 'labels.csv'
            if label.exists():
                with label.open('a', encoding='utf-8', newline='') as f:
                    writer = csv.DictWriter(f, LABEL_FIELDS)
                    import uuid
                    writer.writerow({'session_id': snapshot['session_id'], 'event_id': str(uuid.uuid4()),
                                     'event_type': 'SESSION_INTERRUPTED', 'event_time_utc': snapshot['ended_at_utc'],
                                     'details_json': json.dumps({'reason': reason, 'recovered_on_startup': True}, ensure_ascii=False)})
                    f.flush()
                    os.fsync(f.fileno())
            atomic_json(path, snapshot)
            recovered.append(str(path.parent))
        except (OSError, ValueError) as exc:
            recovered.append(f'恢复失败 {path}: {exc}')
    return recovered
