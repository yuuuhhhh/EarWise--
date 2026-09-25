"""Server-owned experiment state, continuous ingestion and audited transitions."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import platform
import tempfile
import time
import uuid
import importlib.metadata
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import __version__
from .configuration import load_settings, media_catalog, plan_for, validate_subject, validate_manifest
from .device import RealDevice, SimulatedDevice
from .protocol import FrameParser
from .quality import QualityTracker, ALGORITHM_VERSION
from .recorder import Recorder, recover_sessions


class ExperimentError(ValueError):
    pass


class Controller:
    def __init__(self, root: Path, simulate=False, clock=time.monotonic_ns):
        self.root = root
        self.settings = load_settings(root)
        self.simulate = simulate
        self.mode = 'simulation' if simulate else 'real'
        self.clock = clock
        self.anchor_ns = clock()
        self.anchor_utc = datetime.now(timezone.utc)
        self.data_root = root / ('simulation_data' if simulate else 'data')
        self.data_root.mkdir(exist_ok=True)
        self.recovered = recover_sessions(self.data_root)
        self.parser = FrameParser()
        self.quality = QualityTracker(self.settings)
        self.device_status = 'disconnected'
        self.device_detail = ''
        self.device = (SimulatedDevice if simulate else RealDevice)(self.settings, self.on_data, self.on_status)
        self.session = None
        self.recorder = None
        self.row_index = 0
        self.notification_id = 0
        self.last_quality_ns = 0
        self.last_heartbeat_ns = 0
        self.syncs = {}
        self.probes = {}
        self.start_requests = {}
        self.seen_events = set()
        self.ratings = {}
        self.lock = asyncio.Lock()
        self.catalog = []
        self.preflight_errors = []
        self.storage_error = None
        self.clients = set()
        self._device_lock = asyncio.Lock()
        self._loop = None
        self._pending_failure = False
        self._failure_totals = None

    async def initialize(self):
        self._loop = asyncio.get_running_loop()
        try:
            with tempfile.TemporaryFile(dir=self.data_root) as probe:
                probe.write(b'output-readiness')
                probe.flush()
        except OSError as exc:
            self.storage_error = '输出目录不可写：' + str(exc)
        try:
            await asyncio.to_thread(validate_manifest, self.root)
            self.catalog = await asyncio.to_thread(media_catalog, self.root)
        except Exception as exc:
            self.preflight_errors = [str(exc)]

    def utc(self, ns=None):
        return (self.anchor_utc + timedelta(microseconds=((ns if ns is not None else self.clock()) - self.anchor_ns) / 1000)).isoformat()

    @property
    def recording(self):
        return self.session is not None and self.session['status'] == 'recording'

    def on_status(self, status, detail=''):
        previous = self.device_status
        self.device_status, self.device_detail = status, detail
        if status in ('disconnected', 'error'):
            self.parser.reset()
            self.quality.new_epoch('ble_disconnected', self.clock())
            if self.recording:
                self.event('BLE_DISCONNECTED', details={'reason': detail})
                self.schedule_finish('interrupted', '蓝牙连接已断开：' + detail)
        elif status == 'connected' and previous != 'connected':
            self.parser.reset()
            if not self.recording:
                self.quality.reset(self.clock())
            else:
                self.quality.new_epoch('ble_connected', self.clock())

    def writer_error(self, reason):
        if self._loop:
            self._loop.call_soon_threadsafe(self.schedule_finish, 'save_failed', '数据写入失败：' + reason)

    def schedule_finish(self, status, reason):
        if self.recording and not self._pending_failure:
            self._pending_failure = True
            self._failure_totals = self.quality.totals
            # Freeze immediately; queued finalization is protected by the state lock.
            self.session['pending_failure'] = reason
            asyncio.create_task(self._finish_guarded(status, reason))

    async def _finish_guarded(self, status, reason):
        async with self.lock:
            if self.recording:
                await self.finish(status, reason)
        self._pending_failure = False

    def on_data(self, data):
        now = self.clock()
        previous_frame_ns = self.quality.last_frame_monotonic_ns
        deadline_anchor = previous_frame_ns
        if deadline_anchor is None and self.recording:
            deadline_anchor = self.session['started_monotonic_ns']
        if deadline_anchor is not None and now-deadline_anchor > self.settings['data_timeout_seconds']*1e9:
            # A late notification must not conceal a timeout between watchdog ticks.
            self.parser.reset()
            self.quality.new_epoch('data_timeout_on_receive', now)
            if self.recording and not self._pending_failure:
                self.event('DATA_TIMEOUT')
                self.event('DATA_GAP', details={'unknown_missing': True, 'last_received_monotonic_ns': previous_frame_ns})
                self.schedule_finish('interrupted', '脑电数据流中断后收到新数据，本轮不自动续采')
        self.notification_id += 1
        old_errors = self.parser.error_count
        frames = self.parser.feed(bytes(data))
        for index, frame in enumerate(frames):
            info = self.quality.ingest(frame, now)
            if info.get('anomaly_reason'):
                self.parser.reset()
                if self.recording:
                    self.event('SEQUENCE_ANOMALY', details=info)
                    self.event('DATA_GAP', details={'unknown_missing': True, **info})
            elif info.get('missing_frames') and self.recording:
                self.event('DATA_GAP', details={'unknown_missing': False, 'estimated_missing_frames': info['missing_frames'],
                                               'device_seq': frame['device_seq'], 'algorithm_version': ALGORITHM_VERSION})
            if not self.recording or self._pending_failure:
                continue
            s = self.session
            row = dict(frame, **{k: info[k] for k in ('frame_status', 'stream_epoch_id')})
            row.update(session_id=s['session_id'], sample_row_index=self.row_index,
                       notification_id=self.notification_id, frame_in_notification=index,
                       received_at_utc=self.utc(now), received_monotonic_ns=now,
                       elapsed_ms=(now - s['started_monotonic_ns']) / 1e6)
            for ch in (0, 1):
                row[f'channel_{ch}_uv'] = (row[f'channel_{ch}_raw'] * self.settings['uv_scale']) if self.settings.get('uv_verified') else None
            try:
                self.recorder.submit('eeg', row)
                self.row_index += 1
            except OSError as exc:
                self.schedule_finish('save_failed', str(exc))
                break
        if self.recording and self.parser.error_count > old_errors:
            self.event('PARSE_ERROR', details={'invalid_bytes_delta': self.parser.error_count - old_errors,
                                               'invalid_bytes_total': self.parser.error_count})

    def event(self, kind, details=None, client=None, scores=None, ns=None):
        s = self.session
        if not s or not self.recorder:
            return
        received = self.clock()
        event_ns = ns if ns is not None else received
        audit = {}
        if client:
            sync = self.syncs.get(s['controller_id'])
            if sync:
                event_ns = int(client['client_performance_ms'] * 1e6 + sync['offset_ns'])
                audit = {k: client.get(k) for k in ('client_event_id', 'client_performance_ms', 'media_position_ms')}
                audit['sync_uncertainty_ms'] = sync['rtt_ms'] / 2
        trial = s.get('current_trial')
        row = dict(session_id=s['session_id'], event_id=str(uuid.uuid4()), event_type=kind,
                   event_time_utc=self.utc(event_ns), event_monotonic_ns=event_ns,
                   elapsed_ms=(event_ns - s['started_monotonic_ns']) / 1e6,
                   server_received_monotonic_ns=received, last_sample_row_index=self.row_index - 1 if self.row_index else None,
                   stream_epoch_id=self.quality.snapshot(received).get('stream_epoch_id'), stage=s['stage'],
                   details_json=json.dumps(details or {}, ensure_ascii=False, allow_nan=False), **audit)
        if trial:
            row.update(trial_id=trial['trial_id'], trial_order=trial['trial_order'], condition=trial['condition'],
                       video_id=trial['video']['video_id'], video_order_in_trial=1)
        if scores:
            row.update(scores)
        try:
            self.recorder.submit('event', row)
        except OSError as exc:
            self.schedule_finish('save_failed', str(exc))

    def stage(self, stage, client=None, ns=None):
        if self.session['stage']:
            self.event('STAGE_END', client=client, ns=ns)
        self.session['stage'] = stage
        self.recorder.snapshot['current_stage'] = stage
        self.recorder.snapshot['trials'] = copy.deepcopy(self.session['trials'])
        try:
            self.recorder.submit('snapshot', copy.deepcopy(self.recorder.snapshot))
        except OSError as exc:
            self.schedule_finish('save_failed', str(exc))
        self.event('STAGE_START', client=client, ns=ns)
        self.flush()

    def flush(self):
        try:
            self.recorder.submit('flush')
        except OSError as exc:
            self.schedule_finish('save_failed', str(exc))

    def readiness_errors(self):
        errors = list(self.preflight_errors)
        if self.storage_error:
            errors.append(self.storage_error)
        if self.device_status != 'connected':
            errors.append('请先连接耳机并启动数据预览' if not self.simulate else '请先连接模拟数据预览')
        if self.quality.snapshot(self.clock()).get('data_status') != 'receiving':
            errors.append('等待近期有效脑电数据')
        if not self.simulate:
            if not self.settings['channel_mapping']['verified']:
                errors.append('左右耳映射尚未确认，请在 config/settings.json 中核对后重启')
            for field, label in [('commands_verified', '原始模式命令'), ('sample_rate_verified', '名义采样率'), ('saturation_verified', 'ADC 削顶定义')]:
                if not self.settings['validations'].get(field):
                    errors.append(label + '尚未实机验证')
        return errors

    def state(self):
        s = copy.deepcopy(self.session)
        if s:
            s['baseline_remaining_seconds'] = max(0, (s['baseline_end_ns'] - self.clock()) / 1e9) if s['stage'] == 'baseline' else 0
        return dict(state_monotonic_ns=self.clock(), mode=self.mode, device={'status': self.device_status, 'detail': self.device_detail},
                    quality=self.quality.snapshot(self.clock()), config=self.settings, session=s,
                    media_catalog=self.catalog, preflight_errors=self.preflight_errors,
                    readiness_errors=self.readiness_errors(), recovered_sessions=self.recovered)

    @staticmethod
    def identity(body):
        subject = validate_subject(body.get('subject_id', ''))
        day, rnd = body.get('day'), body.get('round')
        if type(day) is not int or day not in (1, 2, 3) or type(rnd) is not int or rnd not in (1, 2, 3, 4):
            raise ExperimentError('请选择合法的天数（1–3）和 round（1–4）')
        return subject, day, rnd

    def attempts(self, subject, day, rnd):
        parent = self.data_root / f'sub_{subject}' / f'day_{day:02}' / f'round_{rnd:02}'
        if not parent.resolve().is_relative_to(self.data_root.resolve()):
            raise ExperimentError('保存路径不合法')
        result = []
        for path in parent.glob('*/config.json'):
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                result.append({**{k: value.get(k) for k in ('session_id', 'started_at_utc', 'attempt_number')}, 'status': value.get('session_status')})
            except (OSError, ValueError):
                result.append({'session_id': path.parent.name, 'status': 'unreadable', 'started_at_utc': ''})
        return sorted(result, key=lambda x: x.get('started_at_utc') or '')

    def control(self, body):
        if not self.session or body.get('session_id') != self.session['session_id']:
            raise ExperimentError('会话编号不匹配')
        if body.get('client_id') != self.session['controller_id']:
            raise ExperimentError('当前页面仅可查看；采集由另一个页面控制')

    async def action(self, name, body):
        cid = body.get('client_id', '')
        if not isinstance(cid, str) or not 8 <= len(cid) <= 100:
            raise ExperimentError('页面标识无效，请刷新设备检查页')
        if name == 'preflight':
            subject, day, rnd = self.identity(body)
            plan = await asyncio.to_thread(plan_for, self.root, day, rnd)
            return dict(plan=plan, previous_attempts=self.attempts(subject, day, rnd), errors=list(self.preflight_errors))
        if name == 'browser_probe':
            probes = body.get('videos', [])
            valid = {}
            for p in probes:
                known = next((v for v in self.catalog if v['video_id'] == p.get('video_id')), None)
                duration = p.get('duration_seconds')
                if known and p.get('playable') is True and isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 and abs(duration - known['duration_seconds']) < 2:
                    valid[p['video_id']] = p
            self.probes[cid] = valid
            if len(valid) != len(self.catalog) or not valid:
                raise ExperimentError('浏览器素材检查未通过：需要完整 16 个视频可解码且时长一致')
            return {}
        if name == 'sync':
            for key in ('client_midpoint_ms', 'server_monotonic_ns', 'rtt_ms'):
                if not isinstance(body.get(key), (float, int)) or not math.isfinite(body[key]):
                    raise ExperimentError('时钟同步数据无效')
            if not 0 <= body['rtt_ms'] <= 2000 or abs(body['server_monotonic_ns'] - self.clock()) > 10e9:
                raise ExperimentError('时钟同步已过期，请重试')
            self.syncs[cid] = dict(offset_ns=body['server_monotonic_ns'] - body['client_midpoint_ms'] * 1e6,
                                   rtt_ms=body['rtt_ms'], updated_ns=self.clock(), **{k: body[k] for k in ('client_midpoint_ms', 'server_monotonic_ns')})
            if self.recording and self.session['controller_id'] == cid:
                self.event('CLOCK_SYNC', details=copy.deepcopy(self.syncs[cid]))
            return {}
        if name in ('connect', 'command'):
            if self.recording or (self.session and self.session['status'] == 'saving'):
                raise ExperimentError('采集期间不能更改设备状态')
            async with self.lock, self._device_lock:
                if self.recording or (self.session and self.session['status'] == 'saving'):
                    raise ExperimentError('采集期间不能更改设备状态')
                if name == 'connect':
                    was_connected = self.device_status == 'connected'
                    await self.device.connect()
                    if not was_connected and self.device_status == 'connected' and (self.simulate or self.settings['validations']['commands_verified']):
                        for command in self.settings['commands']:
                            await self.device.command(command)
                else:
                    command = body.get('command')
                    if command not in ('b', 'S', 'R', 'I'):
                        raise ExperimentError('不支持的设备命令')
                    await self.device.command(command)
            return {}
        async with self.lock:
            if name == 'start':
                return await self.start(body)
            if name == 'heartbeat':
                self.control(body)
                if self.recording:
                    if self.clock()-self.last_heartbeat_ns > self.settings['page_heartbeat_seconds']*1e9:
                        await self.finish('interrupted', '控制页面心跳超时后恢复，本轮不自动续采')
                    else:
                        self.last_heartbeat_ns = self.clock()
                return {}
            if name == 'unload':
                if self.recording and body.get('client_id') == self.session['controller_id'] and body.get('session_id') == self.session['session_id']:
                    await self.finish('interrupted', '控制页面刷新或关闭')
                return {}
            self.control(body)
            if name == 'abort':
                if self.recording:
                    await self.finish('aborted', '被试主动中止本轮')
                return {}
            if name == 'rating':
                return await self.rating(body)
            if name == 'media':
                return await self.media(body)
            raise ExperimentError('未知操作')

    async def start(self, body):
        request_id = body.get('request_id')
        cid = body['client_id']
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ExperimentError('开始请求标识无效')
        request_key = (cid, request_id)
        if request_key in self.start_requests:
            return {'session_id': self.start_requests[request_key]}
        if self.recording or (self.session and self.session['status'] == 'saving'):
            raise ExperimentError('已有会话正在采集或保存')
        subject, day, rnd = self.identity(body)
        errors = self.readiness_errors()
        if errors:
            raise ExperimentError('；'.join(errors))
        if body.get('audio_confirmed') is not True:
            raise ExperimentError('请确认原视频声音可听见且音量合适')
        if len(self.probes.get(cid, {})) != len(self.catalog) or not self.catalog:
            raise ExperimentError('请先完成本页面全部素材播放兼容性检查')
        if cid not in self.syncs:
            raise ExperimentError('请先完成浏览器时钟同步')
        prior = self.attempts(subject, day, rnd)
        if any(p['status'] == 'completed' for p in prior) and body.get('confirm_retry') is not True:
            raise ExperimentError('本轮已有完成记录，请确认重采；新记录不会覆盖原记录')
        plan = await asyncio.to_thread(plan_for, self.root, day, rnd)
        catalog_by_id = {video['video_id']: video for video in self.catalog}
        for item in plan:
            video = item['video']
            original = catalog_by_id.get(video['video_id'], {})
            if any(video.get(key) != original.get(key) for key in ('size_bytes', 'mtime_ns', 'ctime_ns', 'duration_seconds')):
                raise ExperimentError('素材在浏览器检查后发生变化，请重启服务并重新检查：' + video['path'])
        # Hashing may take time: recheck the actual stream after preparation.
        if self.readiness_errors():
            raise ExperimentError('准备素材期间设备状态发生变化，请重新检查数据')
        with tempfile.TemporaryFile(dir=self.data_root) as f:
            f.write(b'write-test')
            f.flush()
        session_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:12]
        trials = [dict(trial_id=f'{session_id}_t{i+1}', trial_order=i+1, condition=p['condition'], video=p['video'],
                       started=False, completed=False, rating_submitted=False) for i, p in enumerate(plan)]
        directory = self.data_root / f'sub_{subject}' / f'day_{day:02}' / f'round_{rnd:02}' / session_id
        snap = copy.deepcopy(self.settings)
        snap['algorithm_versions']['packet_loss'] = ALGORITHM_VERSION
        snap.update(session_id=session_id, subject_id=subject, day=day, round=rnd, attempt_number=len(prior)+1,
                    retry_of_session_id=prior[-1]['session_id'] if prior else None,
                    software_version=__version__, python_version=platform.python_version(), platform=platform.platform(),
                    dependency_versions={name: importlib.metadata.version(name) for name in ('tornado', 'bleak')},
                    mode=self.mode, session_status='recording', trials=copy.deepcopy(trials),
                    audio_confirmed=True, clock_sync=copy.deepcopy(self.syncs[cid]),
                    timing={'method': 'browser RTT midpoint mapped to process monotonic clock',
                            'intervals': '[start,end)', 'sample_times': 'host receipt, not device sample time',
                            'limitation': 'software event markers; physical display timing unverified'},
                    files=['eeg_raw.csv', 'labels.csv', 'config.json'])
        snap['started_at_utc'] = self.utc()
        self.recorder = Recorder(directory, snap, self.writer_error, self.settings['queue_max_items'], self.settings['queue_max_age_seconds'])
        await asyncio.to_thread(self.recorder.initialize)
        now = self.clock()
        self.parser.reset()
        self.quality.reset(now)
        self.row_index = 0
        self.notification_id = 0
        self.seen_events = set()
        self.ratings = {}
        self.last_quality_ns = now
        self.last_heartbeat_ns = now
        self._pending_failure = False
        self._failure_totals = None
        self.session = dict(session_id=session_id, subject_id=subject, day=day, round=rnd, controller_id=cid,
                            status='recording', stage='', trials=trials, trial_index=0, current_trial=None,
                            started_monotonic_ns=now, started_at_utc=self.utc(now), baseline_end_ns=now+int(30e9),
                            output_directory=str(directory), files=snap['files'], reason='')
        snap.update(started_monotonic_ns=now, started_at_utc=self.utc(now), current_stage='baseline')
        self.recorder.submit('snapshot', copy.deepcopy(snap))
        self.start_requests[request_key] = session_id
        self.event('SESSION_START')
        self.stage('baseline', ns=now)
        self.event('BASELINE_START', ns=now)
        try:
            await asyncio.to_thread(self.recorder.barrier)
        except OSError as exc:
            await self.finish('save_failed', str(exc))
            raise ExperimentError('初始化文件持久化失败') from exc
        if self.device_status != 'connected':
            await self.finish('interrupted', '文件初始化期间设备连接断开')
        return {'session_id': session_id}

    def next_trial(self):
        s = self.session
        s['current_trial'] = s['trials'][s['trial_index']]
        s['current_trial']['played_seconds'] = 0.0
        s['current_trial']['playing_since_ns'] = None
        s['current_trial']['stall_since_ns'] = None
        self.stage('transition')

    def validate_media(self, body):
        for key in ('client_performance_ms', 'media_position_ms'):
            if not isinstance(body.get(key), (int, float)) or not math.isfinite(body[key]) or body[key] < 0:
                raise ExperimentError('媒体事件时间无效')
        if not isinstance(body.get('client_event_id'), str) or len(body['client_event_id']) > 100:
            raise ExperimentError('媒体事件标识无效')
        if body.get('playback_rate', 1) != 1:
            raise ExperimentError('实验视频必须以原速播放')
        sync = self.syncs.get(body['client_id'])
        if not sync:
            raise ExperimentError('媒体事件缺少时钟同步')
        mapped = body['client_performance_ms'] * 1e6 + sync['offset_ns']
        if abs(mapped-self.clock()) > 5e9:
            raise ExperimentError('媒体事件时钟偏差超过 5 秒，请重新同步')

    async def media(self, body):
        key = body.get('client_event_id')
        if key in self.seen_events:
            return {}
        s = self.session
        if not self.recording or self._pending_failure:
            raise ExperimentError('本轮已停止，不能继续播放')
        trial = s['current_trial']
        if not trial or body.get('trial_id') != trial['trial_id']:
            raise ExperimentError('媒体事件不属于当前 trial')
        self.validate_media(body)
        kind = body.get('event_type')
        if kind not in ('playing', 'waiting', 'pause', 'ended', 'error'):
            raise ExperimentError('无效媒体事件')
        if trial['completed']:
            return {}
        now = self.clock()
        pos = body['media_position_ms'] / 1000
        duration = trial['video']['duration_seconds']
        if pos > duration + 2:
            raise ExperimentError('媒体位置超过视频时长')
        if kind == 'ended':
            prospective_played = trial['played_seconds'] + ((now-trial['playing_since_ns'])/1e9 if trial['playing_since_ns'] is not None else 0)
            if not trial['started'] or pos < duration-0.75 or prospective_played < duration-1:
                raise ExperimentError('视频尚未完整播放，不能进入量表')
        if kind == 'playing':
            if s['stage'] not in ('transition', 'video_buffering', trial['condition']):
                raise ExperimentError('当前阶段不能播放视频')
            if not trial['started']:
                if pos > 1:
                    raise ExperimentError('视频必须从头播放')
                trial['started'] = True
                self.event('TRIAL_START', client=body)
            if trial['playing_since_ns'] is None:
                trial['playing_since_ns'] = now
            trial['stall_since_ns'] = None
            self.stage(trial['condition'], client=body)
            self.event('VIDEO_PLAYING', client=body)
        else:
            if trial['playing_since_ns'] is not None:
                trial['played_seconds'] += (now-trial['playing_since_ns'])/1e9
                trial['playing_since_ns'] = None
            if kind in ('waiting', 'pause'):
                if trial['stall_since_ns'] is None:
                    trial['stall_since_ns'] = now
                self.event('VIDEO_WAITING' if kind == 'waiting' else 'VIDEO_PAUSED', client=body)
                if trial['started']:
                    self.stage('video_buffering', client=body)
            elif kind == 'error':
                self.event('VIDEO_ERROR', client=body, details={'message': body.get('message', '媒体解码或网络错误')})
                await self.finish('interrupted', '视频播放失败')
            elif kind == 'ended':
                self.event('VIDEO_ENDED', client=body)
                self.event('TRIAL_END', client=body)
                trial['completed'] = True
                trial['stall_since_ns'] = None
                self.stage('questionnaire', client=body)
                self.event('QUESTIONNAIRE_START', client=body)
        self.seen_events.add(key)
        return {}

    async def rating(self, body):
        trial_id = body.get('trial_id')
        if trial_id in self.ratings:
            previous = self.ratings[trial_id]
            if previous != (body.get('score'), body.get('confidence_score')):
                raise ExperimentError('该量表已提交，不能更改已保存评分')
            return {}
        s = self.session
        trial = s['current_trial']
        if not self.recording or self._pending_failure or s['stage'] != 'questionnaire' or not trial or trial_id != trial['trial_id']:
            raise ExperimentError('当前尚未进入该 trial 的量表阶段')
        score, confidence = body.get('score'), body.get('confidence_score')
        if type(score) is not int or type(confidence) is not int or not 1 <= score <= 5 or not 1 <= confidence <= 5:
            raise ExperimentError('两项评分都必须是 1–5 的整数')
        self.event('RATING_SUBMITTED', scores={f"{trial['condition']}_score": score, 'confidence_score': confidence}, details={'request_id': body.get('request_id')})
        try:
            await asyncio.to_thread(self.recorder.barrier)
        except OSError as exc:
            await self.finish('save_failed', str(exc))
            raise ExperimentError('评分写入失败，已停止本轮') from exc
        self.ratings[trial_id] = (score, confidence)
        trial['rating_submitted'] = True
        if s['trial_index'] == 0:
            self.event('STAGE_END')
            s['stage'] = ''
            s['trial_index'] = 1
            self.next_trial()
        else:
            await self.finish('completed', '')
        return {}

    async def tick(self):
        if not self.recording:
            return
        async with self.lock:
            if not self.recording or self._pending_failure:
                return
            now = self.clock()
            s = self.session
            quality = self.quality.snapshot(now)
            if now - self.last_heartbeat_ns > self.settings['page_heartbeat_seconds'] * 1e9:
                await self.finish('interrupted', '控制页面心跳超时，页面可能已关闭或连接中断')
                return
            # After reset allow one timeout interval for the first formal frame.
            if quality.get('data_status') in ('waiting', 'interrupted') and now-s['started_monotonic_ns'] > self.settings['data_timeout_seconds']*1e9:
                self.event('DATA_TIMEOUT')
                self.event('DATA_GAP', details={'unknown_missing': True})
                self.parser.reset()
                self.quality.new_epoch('data_timeout', now)
                await self.finish('interrupted', '脑电数据流中断')
                return
            trial = s.get('current_trial')
            if trial and trial.get('stall_since_ns') is not None and now-trial['stall_since_ns'] > self.settings['video_timeout_seconds']*1e9:
                await self.finish('interrupted', '视频持续暂停或缓冲超过允许时间')
                return
            if now-self.last_quality_ns >= 1e9:
                self.event('QUALITY_SNAPSHOT', details=quality)
                self.last_quality_ns = now
            if s['stage'] == 'baseline' and now >= s['baseline_end_ns']:
                self.event('BASELINE_END', ns=s['baseline_end_ns'])
                self.event('STAGE_END', ns=s['baseline_end_ns'])
                s['stage'] = ''
                self.next_trial()

    async def finish(self, status, reason):
        s = self.session
        if not s or s['status'] != 'recording':
            return
        self.event('QUALITY_SNAPSHOT', details=self.quality.snapshot(self.clock()))
        self.event('STAGE_END', details={'reason': reason})
        if status != 'completed':
            self.event({'aborted': 'SESSION_ABORTED', 'interrupted': 'SESSION_INTERRUPTED', 'save_failed': 'SAVE_ERROR'}[status], details={'reason': reason})
        self.event('SESSION_END', details={'status': status, 'reason': reason})
        s['status'] = 'saving'
        s['reason'] = reason
        snapshot = copy.deepcopy(self.recorder.snapshot)
        snapshot.update(session_status=status, ended_at_utc=self.utc(), ended_monotonic_ns=self.clock(),
                        termination_reason=reason, current_stage=s['stage'], trials=copy.deepcopy(s['trials']),
                        summary={**(self._failure_totals or self.quality.totals), 'actual_rows': self.row_index,
                                 'questionnaires_submitted': len(self.ratings), 'files': s['files']})
        try:
            result = await asyncio.to_thread(self.recorder.finish, snapshot, status == 'completed')
            s['status'] = result['session_status']
            s['reason'] = result['termination_reason']
        except Exception as exc:
            s['status'] = 'save_failed'
            s['reason'] = '保存收尾失败：' + str(exc)
        s['summary'] = snapshot['summary']

    async def close(self):
        async with self.lock:
            if self.recording:
                await self.finish('interrupted', '采集服务关闭')
        await self.device.disconnect()
