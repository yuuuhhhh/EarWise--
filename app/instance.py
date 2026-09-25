"""Identify and reopen this project's already-running local server."""
from __future__ import annotations

import json
import secrets
import webbrowser
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

APP_ID = 'earwise-collector'
INSTANCE_FILE = 'server-instance.json'
MODE_NAMES = {'simulation': '模拟模式', 'real': '真实设备模式'}


class AlreadyRunning(RuntimeError):
    """The project's exclusive process lock is owned by another process."""


def make_instance(port: int, simulate: bool) -> dict:
    return dict(app=APP_ID, mode='simulation' if simulate else 'real',
                instance_id=secrets.token_hex(16), url=f'http://127.0.0.1:{port}')


def write_instance(root: Path, instance: dict) -> None:
    path = root / 'work' / INSTANCE_FILE
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_name(f'{INSTANCE_FILE}.{instance["instance_id"]}.tmp')
    try:
        temporary.write_text(json.dumps(instance, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def clear_instance(root: Path, instance_id: str) -> None:
    path = root / 'work' / INSTANCE_FILE
    try:
        instance = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(instance, dict) and instance.get('instance_id') == instance_id:
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        # Cleanup must not hide the server's original shutdown error.
        pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, 'Instance probe cannot redirect', headers, fp)


def verify_instance(root: Path, timeout: float = 2.0) -> dict:
    """Trust metadata only after checking the same live instance over loopback."""
    try:
        instance = json.loads((root / 'work' / INSTANCE_FILE).read_text(encoding='utf-8'))
        if not isinstance(instance, dict):
            raise ValueError('invalid metadata')
        url = urlparse(instance.get('url', ''))
        if (instance.get('app') != APP_ID or instance.get('mode') not in MODE_NAMES
                or not isinstance(instance.get('instance_id'), str) or not instance['instance_id']
                or url.scheme != 'http' or url.hostname != '127.0.0.1'
                or url.username is not None or url.password is not None
                or url.port is None or not 1 <= url.port <= 65535
                or url.path not in ('', '/') or url.query or url.fragment):
            raise ValueError('invalid metadata')
        # Bypass system proxies and disallow redirects, including to another local port.
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(instance['url'].rstrip('/') + '/api/instance', timeout=timeout) as response:
            data = response.read(4097)
            if len(data) > 4096:
                raise ValueError('invalid response')
            live = json.loads(data)
        if not isinstance(live, dict) or any(
                live.get(key) != instance[key] for key in ('app', 'mode', 'instance_id', 'url')):
            raise ValueError('instance mismatch')
        return instance
    except (OSError, ValueError, TypeError, AttributeError, HTTPException) as exc:
        raise RuntimeError('本项目已有服务占用运行锁，但无法核验该服务。'
                           '可能是旧版服务或服务仍在启动；请查看原启动窗口，'
                           '必要时在原窗口按 Ctrl+C 关闭后重试。') from exc


def open_browser(url: str) -> None:
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        print(f'未能自动打开浏览器，请手动访问：{url}', flush=True)


def reopen_instance(root: Path, args) -> None:
    instance = verify_instance(root)
    requested_mode = 'simulation' if args.simulate else 'real'
    if instance['mode'] != requested_mode:
        raise RuntimeError(f'当前正在运行{MODE_NAMES[instance["mode"]]}：{instance["url"]}。'
                           f'本次请求{MODE_NAMES[requested_mode]}；请先在原启动窗口按 Ctrl+C '
                           '安全关闭，再启动所需模式。')
    print(f'EarWise采集系统已在运行（{MODE_NAMES[instance["mode"]]}）：{instance["url"]}', flush=True)
    if not args.no_browser:
        open_browser(instance['url'])
