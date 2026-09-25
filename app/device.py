"""BLE and explicitly simulated devices sharing synchronous receive callbacks.

Every asynchronous operation stays on the application's running asyncio loop.
Connecting subscribes to notifications only; hardware commands require a separate
explicit call using the project's checked configuration.
"""

import asyncio
import contextlib
import math
import random

from .protocol import encode_frame


class RealDevice:
    def __init__(self, config: dict, on_data, on_status):
        self.config = dict(config)
        self.on_data = on_data
        self.on_status = on_status
        self.status = "disconnected"
        self._client = None
        self._loop = None
        self._lock = asyncio.Lock()

    def _set_status(self, status, detail=""):
        self.status = status
        self.on_status(status, detail)

    def _check_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("蓝牙必须使用同一个 asyncio 事件循环")

    def _on_disconnected(self, client):
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._disconnected_on_loop, client)

    def _disconnected_on_loop(self, client):
        if self._client is client:
            self._client = None
            self._set_status("disconnected", "耳机蓝牙连接已断开")

    def _on_notification(self, _sender, data):
        # Bleak delivers callbacks on the same loop that established connection.
        self.on_data(bytes(data))

    async def connect(self):
        self._check_loop()
        async with self._lock:
            if self._client is not None and self._client.is_connected:
                return
            from bleak import BleakClient, BleakScanner

            client = None
            self._set_status("searching", "正在搜索耳机，请确认已开机且未被旧程序占用")
            try:
                target_name = self.config.get("target_name", "MindBridge-v3.11")
                device = await BleakScanner.find_device_by_filter(
                    lambda dev, advertisement: dev.name == target_name or advertisement.local_name == target_name,
                    timeout=float(self.config.get("scan_timeout_seconds", 10)),
                )
                if device is None:
                    raise RuntimeError(f"未找到设备 {target_name}")
                self._set_status("connecting", f"正在连接 {target_name}")
                client = BleakClient(device, disconnected_callback=self._on_disconnected,
                                     timeout=float(self.config.get("connect_timeout_seconds", 15)))
                self._client = client
                await client.connect()
                service_uuid = self.config.get("service_uuid", "0000ae30-0000-1000-8000-00805f9b34fb")
                if client.services.get_service(service_uuid) is None:
                    raise RuntimeError(f"设备缺少配置的服务 UUID：{service_uuid}")
                await client.start_notify(self.config.get("notify_uuid", "0000ae02-0000-1000-8000-00805f9b34fb"), self._on_notification)
                self._set_status("connected", "耳机已连接，已订阅数据通知")
            except BaseException as exc:
                self._client = None
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.disconnect()
                self._set_status("disconnected", f"蓝牙连接失败：{exc}")
                raise

    async def disconnect(self):
        self._check_loop()
        async with self._lock:
            client, self._client = self._client, None
            try:
                if client is not None and client.is_connected:
                    await client.disconnect()
            finally:
                self._set_status("disconnected", "已断开耳机连接；未发送未经验证的停止命令")

    async def command(self, command: str):
        self._check_loop()
        if command not in ("S", "b", "R", "I"):
            raise ValueError("命令必须是参考脚本中的单字节 S、b、R 或 I；大小写不可替换")
        async with self._lock:
            if self._client is None or not self._client.is_connected:
                raise RuntimeError("耳机未连接，无法发送命令")
            try:
                await self._client.write_gatt_char(
                    self.config.get("write_uuid", "0000ae01-0000-1000-8000-00805f9b34fb"),
                    command.encode("ascii"), response=True,
                )
            except Exception as exc:
                self.on_status(self.status, f"设备命令 {command} 发送失败：{exc}")
                raise


class SimulatedDevice:
    """Reproducible two-channel data; never instantiated implicitly as fallback."""

    def __init__(self, config: dict, on_data, on_status):
        self.config = dict(config)
        self.on_data = on_data
        self.on_status = on_status
        self.status = "disconnected"
        self._task = None
        self._sequence = 0
        self._sample_count = 0
        self._random = random.Random(250)
        self._rate = float(config.get("nominal_sample_rate", 250))
        self._batch = int(config.get("simulation_batch_size", 10))
        if self._rate <= 0 or not 1 <= self._batch <= 100:
            raise ValueError("模拟采样率或批量大小无效")

    async def connect(self):
        if self._task is not None and not self._task.done():
            return
        self.status = "connected"
        self.on_status(self.status, "模拟设备已连接；当前数据不是人体脑电")
        self._task = asyncio.create_task(self._emit(), name="simulated-eeg-device")

    async def _emit(self):
        loop = asyncio.get_running_loop()
        next_batch = loop.time()
        try:
            while True:
                frames = []
                for _ in range(self._batch):
                    t = self._sample_count / self._rate
                    a = round(65000 * math.sin(2 * math.pi * 10 * t) + self._random.gauss(0, 1500))
                    b = round(52000 * math.sin(2 * math.pi * 8 * t + 0.3) + self._random.gauss(0, 1500))
                    frames.append(encode_frame(self._sequence, a, b))
                    self._sequence = (self._sequence + 1) & 0xFF
                    self._sample_count += 1
                self.on_data(b"".join(frames))
                next_batch += self._batch / self._rate
                await asyncio.sleep(max(0, next_batch - loop.time()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.status = "disconnected"
            self.on_status(self.status, f"模拟数据流失败：{exc}")

    async def disconnect(self):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.status = "disconnected"
        self.on_status(self.status, "模拟设备已断开")

    async def command(self, command: str):
        if command not in ("S", "b", "R", "I"):
            raise ValueError("模拟命令必须为 S、b、R 或 I")
        if self.status != "connected":
            raise RuntimeError("模拟设备未连接")
        if command == "R":
            self._sequence = 0
