"""Windows desktop host. Closing the window leaves owned work running in the tray."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

LOG = logging.getLogger(__name__)


class SingleInstance:
    def __init__(self, directory):
        self.directory = directory
        self.file = None
        self.event = None
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateEventW': ([wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            'OpenEventW': ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            'SetEvent': ([wintypes.HANDLE], wintypes.BOOL),
            'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            getattr(self.api, name).argtypes = args
            getattr(self.api, name).restype = result
        key = hashlib.sha256(str(directory.resolve()).casefold().encode()).hexdigest()
        self.name = 'Local\\CairnRestore-' + key

    def acquire(self):
        import msvcrt
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.file = (self.directory / 'launcher.lock').open('a+b')
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            if self.file:
                self.file.close(); self.file = None
            event = self.api.OpenEventW(0x0002, False, self.name)
            if event:
                try:
                    self.api.SetEvent(event)
                finally:
                    self.api.CloseHandle(event)
                return False
            raise RuntimeError('该数据目录已有 Cairn 启动器运行，请先退出原启动器。') from None
        self.event = self.api.CreateEventW(None, False, False, self.name)
        if not self.event:
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        return True

    def watch(self, stop, restore):
        while not stop.is_set():
            if self.api.WaitForSingleObject(self.event, 300) == 0:
                restore()

    def close(self):
        if self.event:
            self.api.CloseHandle(self.event); self.event = None
        if self.file:
            self.file.close(); self.file = None


class DesktopRuntime:
    def __init__(self, config_path, data_dir=None, environment=None):
        from cairn.dispatcher.config import DispatchConfig
        from pydantic import ValidationError
        try:
            self.config = DispatchConfig.load(config_path, environment={**os.environ, **environment} if environment is not None else None)
        except ValidationError as exc:
            raise ValueError('\n'.join('.'.join(map(str, e['loc'])) + ': ' + e['msg']
                                       for e in exc.errors(include_input=False, include_context=False))) from None
        self.config.design = None  # Desktop design always uses the versioned, bundled Skill resources.
        endpoint = urlsplit(self.config.server)
        if (endpoint.scheme != 'http' or endpoint.hostname not in ('localhost', '127.0.0.1')
                or endpoint.username or endpoint.password or endpoint.path not in ('', '/') or endpoint.query or endpoint.fragment):
            raise ValueError('桌面版 server 必须是本机 HTTP 地址，例如 http://127.0.0.1:8000')
        if self.config.runtime.execution != 'local':
            raise ValueError('桌面版需要 runtime.execution: local')
        self.host, self.port = '127.0.0.1', endpoint.port or 80
        self.url = self.config.server.rstrip('/')
        token = Path(self.config.worker_token_file) if self.config.worker_token_file else None
        self.data_dir = Path(data_dir) if data_dir else token.parent if token else config_path.parent / '.cairn' / 'data'
        self.db_path = self.data_dir / (token.with_suffix('.db').name if token else 'cairn.db')
        self.config_path = config_path
        self.stop = threading.Event()
        self.server = self.dispatcher = self.server_thread = self.dispatch_thread = self.socket = None
        self.failure = None

    def start(self):
        import uvicorn
        from cairn.server import db
        from cairn.server.access import credential
        from cairn.dispatcher.scheduler.loop import DispatcherLoop
        # Reserve the endpoint before opening the database; never attach to or stop an unrelated service.
        self.socket = socket.socket()
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.socket.bind((self.host, self.port))
        self.socket.listen(128)
        db.configure(self.db_path)
        os.environ['CAIRN_WORKER_TOKEN'] = credential('worker')
        credential('operator')
        from cairn.server.app import app
        self.server = uvicorn.Server(uvicorn.Config(app, host=self.host, port=self.port, log_config=None, access_log=False))
        self.server_thread = threading.Thread(target=self.server.run, kwargs={'sockets': [self.socket]}, name='cairn-api')
        self.server_thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if not self.server_thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError('本地工作台未能启动，请查看 desktop.log。')
            time.sleep(.05)
        self.dispatcher = DispatcherLoop(self.config_path, config=self.config)
        self.dispatch_thread = threading.Thread(target=self.run_dispatcher, name='cairn-dispatcher')
        self.dispatch_thread.start()

    def run_dispatcher(self):
        try:
            self.dispatcher.run(stop_event=self.stop)
        except Exception:
            LOG.exception('Desktop dispatcher stopped')
            self.failure = '调度器已停止，请在运行设置中修正配置并重新应用，详情见 desktop.log。'

    def reload(self, config_path, environment):
        from cairn.dispatcher.config import DispatchConfig
        candidate = DispatchConfig.load(config_path, environment={**os.environ, **environment})
        candidate.design = None
        if (candidate.server != self.config.server or candidate.worker_token_file != self.config.worker_token_file
                or candidate.local != self.config.local or candidate.runtime.execution != self.config.runtime.execution):
            raise ValueError('热加载只修改供应商、Agent 和调度参数；运行地址与存储位置保持不变。')
        if self.dispatch_thread and self.dispatch_thread.is_alive():
            self.dispatcher.reload_config(candidate)
        else:
            from cairn.dispatcher.scheduler.loop import DispatcherLoop
            self.dispatcher = DispatcherLoop(config_path, config=candidate)
            self.stop.clear()
            self.failure = None
            self.dispatch_thread = threading.Thread(target=self.run_dispatcher, name='cairn-dispatcher')
            self.dispatch_thread.start()
        self.config = candidate

    def shutdown(self):
        self.stop.set()
        if self.dispatcher:
            self.dispatcher.wake.set()
        # Keep the API alive until workers have persisted cancellation and evidence.
        if self.dispatch_thread:
            self.dispatch_thread.join()
        elif self.dispatcher:
            self.dispatcher.close()
        if self.server:
            self.server.should_exit = True
        if self.server_thread:
            self.server_thread.join()
        if self.socket:
            self.socket.close(); self.socket = None


class DesktopWindow:
    def __init__(self, window, tray, runtime, extra_windows=None):
        self.window, self.tray, self.runtime = window, tray, runtime
        self.extra_windows = extra_windows if extra_windows is not None else []
        self.exiting = threading.Event()

    def restore(self, *_):
        self.window.show()
        self.window.restore()

    def closing(self):
        if self.exiting.is_set():
            return True
        self.window.hide()
        return False

    def exit(self, *_):
        if self.exiting.is_set():
            return
        self.exiting.set()
        self.tray.title = 'Cairn · 正在保存并退出'
        def finish():
            try:
                if self.runtime:
                    self.runtime.shutdown()
            except Exception:
                LOG.exception('Desktop shutdown failed')
                self.exiting.clear()
                self.tray.title = 'Cairn · 退出未完成'
                self.tray.notify('退出未完成，请查看 desktop.log 后重试。', 'Cairn')
                return
            self.tray.stop()
            for window in self.extra_windows:
                window.destroy()
            self.window.destroy()
        threading.Thread(target=finish, name='cairn-exit').start()


def main():
    if os.name != 'nt':
        raise RuntimeError('This desktop build targets Windows')
    home = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path)
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    runtime = instance = tray = None
    listener_stop = threading.Event()
    listener = None
    startup_error = ''
    try:
        from cairn.desktop_settings import DesktopSettings
        settings = DesktopSettings(home, args.config)
        local = settings.local
        for name, value in local.get('environment', {}).items():
            os.environ[name] = str(value)
        os.environ['NO_PROXY'] = ','.join(filter(None, [os.environ.get('NO_PROXY', ''), 'localhost', '127.0.0.1']))
        try:
            runtime = DesktopRuntime(settings.config_path, args.data_dir)
        except (OSError, ValueError):
            startup_error = '运行配置尚未就绪，请填写设置并保存后启动。'
        directory = runtime.data_dir if runtime else args.data_dir or home / '.cairn/data'
        instance = SingleInstance(directory)
        if not instance.acquire():
            return
        logging.basicConfig(filename=directory / 'desktop.log', encoding='utf-8', level=logging.INFO,
                            format='%(asctime)s %(levelname)s %(name)s %(message)s', force=True)
        if runtime:
            try:
                runtime.start()
            except Exception:
                LOG.exception('Desktop runtime startup failed; opening settings')
                startup_error = '工作台启动失败，请检查服务端口和运行配置，详细信息见 desktop.log。'
                runtime.shutdown()
                runtime = None
        settings.startup_error = startup_error
        import webview
        import pystray
        from PIL import Image
        assets = Path(__file__).parent / 'server/static'
        image = Image.open(assets / 'assets/cairn.png')
        settings_url = (assets / 'desktop-settings.html').as_uri()
        window = webview.create_window('Cairn' if runtime else 'Cairn · 设置', runtime.url if runtime else settings_url,
                                       width=1400 if runtime else 1000, height=900, min_size=(800, 600), text_select=True)
        tray = pystray.Icon('Cairn', image, 'Cairn · 后台运行')
        extra_windows = []
        controller = DesktopWindow(window, tray, runtime, extra_windows)
        def expose_settings(target):
            target.expose(settings.get_settings, settings.save_settings)
        def close_settings(target):
            def closing():
                if controller.exiting.is_set():
                    return True
                target.hide()
                return False
            target.events.closing += closing
        def open_settings():
            if not runtime:
                controller.restore()
            elif extra_windows:
                extra_windows[0].show(); extra_windows[0].restore()
            else:
                target = webview.create_window('Cairn · 设置', settings_url, width=1000, height=850, min_size=(800, 600), text_select=True)
                expose_settings(target)
                close_settings(target)
                extra_windows.append(target)
            return {'ok': True}
        def apply_settings(config_path, environment):
            nonlocal runtime, window
            if controller.exiting.is_set():
                raise ValueError('Cairn 正在退出，请稍后再修改设置。')
            if runtime:
                runtime.reload(config_path, environment)
                return
            candidate = DesktopRuntime(config_path, args.data_dir, environment=environment)
            try:
                candidate.start()
                target = webview.create_window('Cairn', candidate.url, width=1400, height=900, min_size=(800, 600), text_select=True, hidden=True)
                target.expose(open_settings)
                target.events.closing += controller.closing
            except Exception:
                candidate.shutdown()
                raise
            runtime = candidate
            controller.runtime = candidate
            previous = window
            previous.events.closing -= controller.closing
            close_settings(previous)
            extra_windows.append(previous)
            window = target
            controller.window = window
            previous.hide()
            window.show()
        settings.apply_config = apply_settings
        if not runtime:
            expose_settings(window)
        else:
            window.expose(open_settings)
        tray.menu = pystray.Menu(pystray.MenuItem('打开 Cairn', controller.restore, default=True),
                                pystray.MenuItem('退出 Cairn', controller.exit))
        window.events.closing += controller.closing
        tray.run_detached()
        def monitor():
            previous_failure = None
            while not listener_stop.wait(.5):
                failure = runtime.failure if runtime else None
                if failure and failure != previous_failure:
                    tray.notify(failure, 'Cairn 调度器异常')
                    tray.title = 'Cairn · 调度器已停止'
                elif not failure and previous_failure:
                    tray.title = 'Cairn · 后台运行'
                previous_failure = failure
        def started():
            nonlocal listener
            listener = threading.Thread(target=instance.watch, args=(listener_stop, controller.restore), daemon=True)
            listener.start()
            threading.Thread(target=monitor, daemon=True).start()
        if startup_error:
            LOG.warning(startup_error)
        webview.start(started, gui='edgechromium', icon=str(assets / 'assets/cairn.ico'))
    except Exception as exc:
        LOG.exception('Cairn desktop startup failed')
        ctypes.windll.user32.MessageBoxW(None, str(exc), 'Cairn 启动失败', 0x10)
    finally:
        listener_stop.set()
        if listener:
            listener.join()
        if tray:
            tray.stop()
        try:
            if runtime:
                runtime.shutdown()
        finally:
            if instance:
                instance.close()


if __name__ == '__main__':
    main()
