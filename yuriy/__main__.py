"""Windows-friendly launcher. Default bind is loopback only."""
import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
import uvicorn
from .app import create_app, config_dir


def request(url, token=None, method='GET'):
    headers = {'X-Yuriy-Token': token} if token else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method), timeout=2) as r:
        return json.load(r)


def main():
    parser = argparse.ArgumentParser(description='Юрий — локальная служба M4')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--workspace', help='Absolute workspace path, development/testing only')
    parser.add_argument('--config', help='Configuration file path')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--preview', action='store_true', help='DEVELOPMENT ONLY: binds 0.0.0.0, permits Arena preview host')
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    cfg = Path(args.config) if args.config else config_dir()/'config.json'
    runfile = cfg.with_name('running.json')
    if args.stop:
        try:
            info = json.loads(runfile.read_text('utf-8'))
            if info['port'] < 1024 or info['port'] > 65535: raise ValueError('port')
            reply = request(f'http://127.0.0.1:{info["port"]}/api/shutdown', info['token'], 'POST')
            print('Запрошена остановка службы. Закройте вкладку браузера.')
        except Exception:
            print('Не удалось остановить службу. Если её окно открыто, нажмите в нём Ctrl+C.')
            return 1
        return 0
    if not 1024 <= args.port <= 65535:
        print('Порт должен быть от 1024 до 65535.'); return 1
    url = f'http://127.0.0.1:{args.port}'
    # Do not kill or reuse arbitrary programs on the requested port.
    with socket.socket() as sock:
        occupied = sock.connect_ex(('127.0.0.1', args.port)) == 0
    if occupied:
        try:
            info = request(url+'/api/health')
            if info.get('app') != 'yuriy-local': raise ValueError('foreign server')
            print('Юрий уже запущен. Открываю существующее окно.')
            if not args.no_browser: webbrowser.open(url)
            return 0
        except Exception:
            print(f'Порт {args.port} занят другой программой. Ничего не остановлено. Выберите --port.'); return 1
    try:
        app = create_app(config_path=cfg, workspace=args.workspace, preview=args.preview)
    except Exception as e:
        print(f'Не удалось открыть приложение: {e}'); return 1
    server = uvicorn.Server(uvicorn.Config(app, host='0.0.0.0' if args.preview else '127.0.0.1', port=args.port, access_log=False, log_level='warning', timeout_graceful_shutdown=30))
    app.state.runtime['shutdown'] = lambda: setattr(server, 'should_exit', True)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    runfile.write_text(json.dumps({'port': args.port, 'pid': os.getpid(), 'token': app.state.test_token}), 'utf-8')
    try:
        if os.name != 'nt': runfile.chmod(0o600)
        print('Юрий M4: '+url, flush=True)
        print('Оставьте это окно открытым. Остановка: Ctrl+C или stop.bat.', flush=True)
        if args.preview: print('ВНИМАНИЕ: включён режим предпросмотра разработчика, не использовать в пользовательской поставке.', flush=True)
        if not args.no_browser:
            def open_when_ready():
                for _ in range(40):
                    try:
                        request(url+'/api/health'); webbrowser.open(url); return
                    except Exception: time.sleep(.25)
            threading.Thread(target=open_when_ready, daemon=True).start()
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        if runfile.exists():
            try:
                if json.loads(runfile.read_text('utf-8'))['pid'] == os.getpid(): runfile.unlink()
            except (OSError, ValueError, KeyError): pass
    return 0

if __name__ == '__main__':
    sys.exit(main())
