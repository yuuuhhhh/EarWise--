"""Run with the project virtual environment: python -m app.main [--simulate]."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import json
import logging
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse

import tornado.httpserver
import tornado.web
import tornado.websocket

from .controller import Controller, ExperimentError
from .instance import AlreadyRunning, clear_instance, make_instance, open_browser, reopen_instance, write_instance

ROOT = Path(__file__).resolve().parents[1]


class LocalOnly:
    def check_local(self):
        if self.request.remote_ip not in ('127.0.0.1', '::1'):
            raise tornado.web.HTTPError(403)
        host = self.request.host.split(':')[0]
        if host not in ('127.0.0.1', 'localhost'):
            raise tornado.web.HTTPError(403)
        origin = self.request.headers.get('Origin')
        if origin and urlparse(origin).netloc != self.request.host:
            raise tornado.web.HTTPError(403)


class ApiHandler(LocalOnly, tornado.web.RequestHandler):
    def initialize(self, controller, instance=None):
        self.controller = controller
        self.instance = instance

    def prepare(self):
        self.check_local()
        self.set_header('Cache-Control', 'no-store')

    async def get(self, action):
        if action == 'state':
            self.write(self.controller.state())
        elif action == 'clock':
            self.write(dict(server_monotonic_ns=self.controller.clock(), server_utc=self.controller.utc()))
        elif action == 'instance' and self.instance is not None:
            self.write(self.instance)
        else:
            raise tornado.web.HTTPError(404)

    async def post(self, action):
        try:
            body = json.loads(self.request.body or b'{}')
            if not isinstance(body, dict):
                raise ExperimentError('请求内容必须是对象')
            result = await self.controller.action(action, body)
            self.write(dict(ok=True, state=self.controller.state(), **result))
        except (ValueError, OSError, RuntimeError) as exc:
            self.set_status(400)
            self.write(dict(ok=False, error=str(exc), state=self.controller.state()))
        except Exception:
            logging.exception('API error: %s', action)
            self.set_status(500)
            self.write(dict(ok=False, error='服务内部错误，请查看启动窗口日志', state=self.controller.state()))


class StateSocket(LocalOnly, tornado.websocket.WebSocketHandler):
    def initialize(self, controller):
        self.controller = controller

    def open(self):
        self.check_local()
        self.controller.clients.add(self)
        self.write_message({'type': 'state', 'state': self.controller.state()})

    async def on_message(self, message):
        try:
            value = json.loads(message)
            if not isinstance(value, dict):
                return
            if value.get('action', value.get('type')) == 'heartbeat':
                payload = value.get('payload', value)
                if isinstance(payload, dict):
                    await self.controller.action('heartbeat', dict(payload, client_id=self.get_argument('client_id', '')))
        except (ValueError, KeyError):
            pass

    def on_close(self):
        self.controller.clients.discard(self)


class LocalStatic(LocalOnly, tornado.web.StaticFileHandler):
    def prepare(self):
        self.check_local()

    def set_extra_headers(self, path):
        self.set_header('X-Content-Type-Options', 'nosniff')
        if path.endswith(('.html', '.js', '.css')):
            self.set_header('Cache-Control', 'no-cache')

    def compute_etag(self):
        # Tornado's process-wide static hash cache can otherwise retain an old
        # UI version during local edits even when the browser revalidates.
        path = getattr(self, 'absolute_path', None)
        if path:
            stat = os.stat(path)
            return f'"{stat.st_mtime_ns:x}-{stat.st_ctime_ns:x}-{stat.st_size:x}"'
        return None


def make_app(controller, instance=None):
    return tornado.web.Application([
        (r'/api/([a-z_]+)', ApiHandler, {'controller': controller, 'instance': instance}),
        (r'/ws', StateSocket, {'controller': controller}),
        (r'/static/(.*)', LocalStatic, {'path': str(ROOT / 'web')}),
        (r'/media/(attention_video/0[1-6]\.mp4|relax_video/0[1-2]\.mp4)', LocalStatic, {'path': str(ROOT)}),
        (r'/(.*)', LocalStatic, {'path': str(ROOT / 'web'), 'default_filename': 'index.html'}),
    ], websocket_ping_interval=10, websocket_ping_timeout=10, websocket_max_message_size=16384,
        serve_traceback=False)


@contextlib.contextmanager
def process_lock(root):
    """Lock before recovery so a second server cannot interrupt the first's files."""
    (root / 'work').mkdir(exist_ok=True)
    with (root / 'work' / 'server.lock').open('a+b') as handle:
        handle.seek(0)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'1')
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise AlreadyRunning('本项目已有采集服务运行。') from exc
            raise
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == 'win32':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


async def run(args):
    controller = Controller(ROOT, args.simulate)
    await controller.initialize()
    instance = make_instance(args.port, args.simulate)
    server = tornado.httpserver.HTTPServer(make_app(controller, instance), max_body_size=1024*1024)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    def shutdown(*_):
        loop.call_soon_threadsafe(stop.set)
    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, shutdown)
    last_publish = 0
    try:
        server.listen(args.port, address='127.0.0.1')
        write_instance(ROOT, instance)
        url = instance['url']
        print(f'EarWise采集系统 | {"模拟模式（非正式数据）" if args.simulate else "真实设备模式"}', flush=True)
        print(f'打开浏览器：{url}\n按 Ctrl+C 安全关闭。', flush=True)
        if not args.no_browser:
            open_browser(url)
        while not stop.is_set():
            await controller.tick()
            now = loop.time()
            if now-last_publish >= 1:
                state = {'type': 'state', 'state': controller.state()}
                for client in list(controller.clients):
                    try:
                        await client.write_message(state)
                    except tornado.websocket.WebSocketClosedError:
                        controller.clients.discard(client)
                last_publish = now
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
    finally:
        server.stop()
        try:
            await controller.close()
            for client in list(controller.clients):
                client.close()
            await server.close_all_connections()
        finally:
            clear_instance(ROOT, instance['instance_id'])


def parse_port(value):
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('端口必须是 1–65535 的整数') from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('端口必须是 1–65535 的整数')
    return port


def main():
    parser = argparse.ArgumentParser(description='EarWise采集系统：本地双通道耳机脑电采集')
    parser.add_argument('--simulate', action='store_true', help='使用明确标记的模拟数据；输出 simulation_data')
    parser.add_argument('--port', type=parse_port, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        try:
            with process_lock(ROOT):
                asyncio.run(run(args))
        except AlreadyRunning:
            reopen_instance(ROOT, args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'启动失败：{exc}', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
