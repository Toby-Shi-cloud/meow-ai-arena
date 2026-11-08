#!/usr/bin/env python3
"""Host controller; Python standard library only. Never opens an interactive TTY."""
import argparse
import base64
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
MODEL = 'codex/gpt-6-astra'
GATEWAY_ORIGIN = 'http://localhost:3425'
SANDBOX_CODEX_HOME = '/home/agent/.soccer-codex'
CONFIG = '''model = "codex/gpt-6-astra"
model_provider = "local_relay"
model_reasoning_effort = "high"
approval_policy = "never"
sandbox_mode = "workspace-write"
web_search = "disabled"
[sandbox_workspace_write]
network_access = true
[model_providers.local_relay]
name = "Mac loopback relay"
base_url = "http://localhost:2345/v1"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
'''


def run(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def gateway_api_key():
    key = os.environ.get('SOCCER_GATEWAY_API_KEY')
    if not key:
        config = ROOT / '.soccer-runs/gateway.json'
        if config.exists():
            key = json.loads(config.read_text()).get('api_key')
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError('请设置 SOCCER_GATEWAY_API_KEY 或 .soccer-runs/gateway.json 的 api_key。')
    return key


def chat(state, role, action, values):
    """DB lives on the Mac, outside all sandbox mounts. Sender never user supplied."""
    with contextlib.closing(sqlite3.connect(state / 'chat.sqlite', timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, sender TEXT, at REAL, body TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS cursors (role TEXT PRIMARY KEY, pos INTEGER)')
        db.execute('BEGIN IMMEDIATE')
        if action == 'send':
            message = values.get('message')
            if not isinstance(message, str) or not 0 < len(message) <= 32000:
                raise ValueError('message must be 1..32000 characters')
            row = db.execute('INSERT INTO messages(sender,at,body) VALUES(?,?,?)', (role, time.time(), message))
            result = {'sent': row.lastrowid}
        elif action in ('inbox', 'log', 'status'):
            row = db.execute('SELECT pos FROM cursors WHERE role=?', (role,)).fetchone()
            cursor = row['pos'] if row else 0
            if action == 'log':
                count = max(0, min(int(values.get('last', 20)), 10000))
                rows = db.execute('SELECT * FROM messages ORDER BY id DESC LIMIT ?', (count or 10000,)).fetchall()[::-1]
            else:
                rows = db.execute('SELECT * FROM messages WHERE sender != ? AND id > ? ORDER BY id', (role, cursor)).fetchall()
            result = [dict(r) for r in rows]
            if action == 'inbox' and rows:
                db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)', (role, rows[-1]['id']))
            if action == 'status':
                result = {'role': role, 'unread': len(rows), 'teammate_submission': '/teammate/submission' if role == 'agent' else str(state.parent / 'agent/submission')}
        elif action == 'clock':
            phase = json.loads((state / 'phase.json').read_text())
            result = {**phase, 'remaining_seconds': max(0, int(phase['deadline'] - time.time()))}
        else:
            raise ValueError('unknown chat action')
        db.commit()
        return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def relay_packet(packet, send, state):
    ident = packet['id']
    def emit(**data):
        send({'id': ident, **data})
    started = False
    try:
        body = base64.b64decode(packet.get('body', ''), validate=True)
        path, method = packet['path'], packet['method']
        if path == '/teamchat' and method == 'POST':
            data = json.loads(body)
            response = json.dumps(chat(state, 'agent', data.pop('action'), data), ensure_ascii=False).encode()
            emit(kind='headers', status=200, type='application/json')
            started = True
            emit(kind='data', data=base64.b64encode(response).decode())
        else:
            # Fixed routes, fixed upstream, no redirect/proxy/forwarded auth or URL.
            if (method, path) not in {('POST', '/v1/responses'), ('POST', '/v1/responses/compact'), ('GET', '/v1/models')}:
                raise ValueError('route forbidden')
            if method == 'POST':
                data = json.loads(body)
                if data.get('model') != MODEL:
                    raise ValueError('only the configured model is allowed')
                data['reasoning'] = {**data.get('reasoning', {}), 'effort': 'high'}
                body = json.dumps(data).encode()
            req = urllib.request.Request(GATEWAY_ORIGIN + path, data=body if method == 'POST' else None,
                method=method, headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + gateway_api_key()})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            try:
                response = opener.open(req, timeout=600)
            except urllib.error.HTTPError as e:
                response = e
            with response:
                emit(kind='headers', status=response.status, type=response.headers.get('Content-Type', 'application/json'))
                started = True
                while chunk := response.read1(16384):
                    emit(kind='data', data=base64.b64encode(chunk).decode())
    except Exception as e:
        if not started:
            emit(kind='headers', status=502, type='application/json')
        emit(kind='data', data=base64.b64encode(json.dumps({'error': str(e)}).encode()).decode())
    finally:
        emit(kind='end')


def bridge(name, state):
    proc = subprocess.Popen(['sbx', 'exec', '-i', name, 'python3', '/opt/soccer-team/tunnel.py'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    lock = threading.Lock()
    def send(data):
        with lock:
            proc.stdin.write(json.dumps(data) + '\n')
            proc.stdin.flush()
    def pump():
        slots = threading.Semaphore(8)
        def handle(packet):
            try:
                relay_packet(packet, send, state)
            finally:
                slots.release()
        for line in proc.stdout:
            try:
                packet = json.loads(line)
                slots.acquire()
                threading.Thread(target=handle, args=(packet,), daemon=True).start()
            except ValueError:
                print('Invalid tunnel packet', file=sys.stderr)
    threading.Thread(target=pump, daemon=True).start()
    return proc


def prepare(home):
    required = ('template.cpp', 'soccer.hpp', 'soccer_engine.py', 'build.py', 'match.py',
                'replay.py', 'viewer.html', 'run.sh', 'RULES.md', 'RULES_en.md',
                'README.md', 'README_en.md', 'sample-replay.txt')
    if any(not (ROOT / 'unix_workspace' / name).is_file() for name in required):
        run(sys.executable, ROOT / 'build_unix_workspace.py')
    home.mkdir(parents=True, exist_ok=True)
    state = home / 'state'
    state.mkdir(exist_ok=True)
    for role in ('mac', 'agent'):
        dest = home / role
        if not dest.exists():
            excluded = ['build', 'replays', '__pycache__']
            if role == 'mac':
                excluded += ['arena', 'bots']
            shutil.copytree(ROOT / 'unix_workspace', dest, ignore=shutil.ignore_patterns(*excluded))
            (dest / 'submission').mkdir()
            shutil.copy2(dest / 'template.cpp', dest / 'submission/player.cpp')
        for filename in ('tc.py', 'clock.py'):
            shutil.copy2(HERE / filename, dest / filename)
        config = {'role': role}
        if role == 'mac':
            config.update(controller=str(HERE), state=str(state))
        (dest / 'team.json').write_text(json.dumps(config))
        (dest / 'TEAMCHAT_README.md').write_text('使用 python3 tc.py send "消息"、inbox、wait 60、log、status。\n仅通过脚本通信；身份由宿主中继固定。python3 clock.py 查看阶段。\n')
    mac = home / 'mac'
    from native import prepare_native
    prepare_native(ROOT, mac)
    shutil.copy2(HERE / 'dev.py', mac / 'dev.py')
    launcher = mac / 'dev'
    launcher.write_text('#!/bin/sh\nset -eu\nWORKSPACE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nexec python3 "$WORKSPACE/dev.py" "$@"\n')
    launcher.chmod(0o755)
    editor_flags = mac / 'compile_flags.txt'
    if not editor_flags.exists():
        editor_flags.write_text(f'-std=c++17\n-I{mac}\n')
    shutil.copy2(HERE / 'MAC_README.md', mac / 'README.md')
    tools_readme = (ROOT / 'unix_workspace/README.md').read_text()
    tools_readme = tools_readme.replace('第一次使用先运行 `python3 build_reference.py`，编译裁判和三个参考 bot（源码在 `../sparring/`）。',
        '初始化脚本已编译好本机原生裁判和三个参考 bot，无需另外构建。')
    (mac / 'TOOLS.md').write_text(tools_readme)
    phase_file = state / 'phase.json'
    if not phase_file.exists():
        phase_file.write_text(json.dumps({'phase': 'meeting', 'deadline': time.time() + 300}))
    shutil.copy2(HERE / 'prompt.txt', home / 'prompt-start.txt')
    for phase in ('first', 'second'):
        shutil.copy2(HERE / f'{phase}.txt', home / f'prompt-{phase}.txt')
    # Agent only sees its own prompt, not controller state or Mac workspace.
    shutil.copy2(home / 'prompt-start.txt', home / 'agent/TASK.txt')


def start(a, home):
    prepare(home)
    state = home / 'state'
    if (state / 'created').exists():
        configure(a, home)
        doctor(a, home)
        if not a.no_agent:
            launch(a, home)
        return
    run('sbx', '--no-input', 'create', 'codex', home / 'agent',
        f'{home / "mac/submission"}:/teammate/submission:ro', '--name', a.name,
        '--cpus', '2', '--memory', '4g',
        '--deny-network', '**', '--skills', 'off', '--static-mcp', 'none', '--kit', HERE / 'toolchain')
    configure(a, home)
    (state / 'created').touch()
    doctor(a, home)
    if not a.no_agent:
        launch(a, home)


def configure(a, home):
    run('sbx', 'exec', '-u', 'root', a.name, 'ln', '-sfn', home / 'agent', '/workspace')
    run('sbx', 'exec', '-u', 'root', a.name, 'mkdir', '-p', '/opt/soccer-team')
    run('sbx', 'cp', HERE / 'tunnel.py', f'{a.name}:/opt/soccer-team/tunnel.py')
    setup = 'from pathlib import Path; p=Path(' + repr(SANDBOX_CODEX_HOME) + '); p.mkdir(exist_ok=True); (p/"config.toml").write_text(' + repr(CONFIG) + ')'
    run('sbx', 'exec', a.name, 'python3', '-c', setup)
    ensure_agent_bots(a.name)


def ensure_agent_bots(name, force=False):
    # Sources are streamed into temporary storage, never mounted into the agent workspace.
    # Existing compatible ELF artifacts are reused, including the user's supplied bots.
    probe = '''import platform, pathlib, sys
machine = {'aarch64': 183, 'x86_64': 62}[platform.machine()]
for name in ('baseline', 'midfield', 'sparring'):
 p = pathlib.Path('/workspace/bots') / name
 if not p.is_file(): sys.exit(1)
 header = p.read_bytes()[:20]
 if header[:4] != b'\\x7fELF' or int.from_bytes(header[18:20], 'little') != machine: sys.exit(1)
'''
    if not force and subprocess.run(['sbx', 'exec', name, 'python3', '-c', probe]).returncode == 0:
        return
    sources = {
        'bot_main.cpp': (ROOT / 'sparring/bot_main.cpp').read_text(),
        'bots.hpp': (ROOT / 'sparring/bots.hpp').read_text().replace('"../workspace/soccer.hpp"', '"soccer.hpp"'),
        'soccer.hpp': (ROOT / 'unix_workspace/soccer.hpp').read_text(),
    }
    build = '''import json, pathlib, subprocess, sys, tempfile, shutil
sources = json.load(sys.stdin)
target = pathlib.Path('/workspace/bots')
target.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='soccer-bots-') as tmp:
 work = pathlib.Path(tmp)
 for name, text in sources.items(): (work / name).write_text(text)
 for name, variant in {'baseline':'plan16', 'midfield':'plan32_b1.5', 'sparring':'pro_b0_n128'}.items():
  subprocess.run(['g++', '-std=c++17', '-O2', '-pthread', '-DBOT_NAME="' + variant + '"', 'bot_main.cpp', '-o', name], cwd=work, check=True, timeout=300)
 for name in ('baseline', 'midfield', 'sparring'):
  staged = target / (name + '.tmp')
  shutil.copy2(work / name, staged)
  staged.replace(target / name)
print('Debian reference bots rebuilt successfully')
'''
    run('sbx', 'exec', '-i', name, 'python3', '-c', build, input=json.dumps(sources), text=True)


def doctor(a, home):
    resource_check = '''import os, pathlib
cpus = os.cpu_count()
mem_kib = int(pathlib.Path('/proc/meminfo').read_text().splitlines()[0].split()[1])
print(f'Sandbox resources: {cpus} CPUs, {mem_kib / 1048576:.2f} GiB visible RAM')
assert cpus <= 2 and mem_kib <= 4 * 1048576, 'Resource limit exceeds 2 CPUs / 4 GiB; use team.py recreate'
'''
    run('sbx', 'exec', a.name, 'python3', '-c', resource_check)
    for target in ('https://example.com', 'https://api.openai.com', 'http://host.docker.internal:3425', '1.1.1.1:443'):
        result = subprocess.run(['sbx', 'policy', 'check', 'network', '--sandbox', a.name, '--json', target], capture_output=True, text=True)
        print(result.stdout)
        value = json.loads(result.stdout)
        if value.get('allowed') is not False or value.get('deny_kind') != 'explicit':
            raise RuntimeError('未确认网络拒绝，停止启动')
    # Read-only checks run as root too: chmod alone is not considered isolation.
    check = '''from pathlib import Path
p=Path('/teammate/submission/.write-probe')
try:
 p.write_text('probe')
except OSError as e:
 assert e.errno == 30, repr(e)
else:
 p.unlink(); raise RuntimeError('teammate mount is writable')
assert not Path(HOST_README).exists(), 'host repository exposed'
'''.replace('HOST_README', repr(str(ROOT / 'README.md')))
    run('sbx', 'exec', '-u', 'root', a.name, 'python3', '-c', check)
    run('sbx', 'exec', a.name, 'sh', '-c', 'command -v codex && command -v python3 && command -v g++ && uname -m')
    run('sbx', 'exec', '-w', '/workspace', a.name, 'python3', 'match.py', 'check', 'template.cpp')


def launch(a, home):
    state = home / 'state'
    gateway_api_key()
    try:
        with socket.create_connection(('localhost', 3425), timeout=3):
            pass
    except OSError as e:
        raise RuntimeError('Mac 模型服务 http://localhost:3425/v1 未就绪；启动服务后重跑本命令。') from e
    # flock is released by the OS on crash; never trust/reuse a stale PID file.
    with (state / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('队友已运行；请用 tc.py 发消息。')
        phase_file = state / 'phase.json'
        phase = json.loads(phase_file.read_text())
        if phase['phase'] == 'meeting' and not (state / 'worker.log').exists():
            phase_file.write_text(json.dumps({'phase': 'meeting', 'deadline': time.time() + 300}))
        (state / 'exit.json').unlink(missing_ok=True)
        with (state / 'worker.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, str(HERE / 'team.py'), '--name', a.name, 'worker', '--lock-fd', str(lock.fileno())],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, pass_fds=(lock.fileno(),))
    print(f'队友后台进程 {proc.pid}；日志 {state / "worker.log"}\n沙箱开赛提示词：{home / "prompt-start.txt"}')
    print(f'Mac 工作区：{home / "mac"}\n在该目录使用 ./dev check、./dev test、./dev chat。')


def worker(a, home):
    state = home / 'state'
    tunnel = bridge(a.name, state)
    try:
        for _ in range(30):
            if tunnel.poll() is not None:
                raise RuntimeError('tunnel exited')
            probe = subprocess.run(['sbx', 'exec', a.name, 'python3', '-c',
                'import urllib.request; print(urllib.request.build_opener(urllib.request.ProxyHandler({})).open("http://127.0.0.1:2345/health", timeout=2).read())'], capture_output=True)
            if probe.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError('tunnel not ready')
        args = ['sbx', 'exec', '-i', '-w', '/workspace', '-e', f'CODEX_HOME={SANDBOX_CODEX_HOME}',
                '-e', 'OPENAI_API_KEY=*', '-e', 'NO_PROXY=localhost,127.0.0.1', a.name, 'codex', 'exec',
                '--skip-git-repo-check', '--json', '-o', '/workspace/last-response.txt', '-']
        phase = json.loads((state / 'phase.json').read_text())['phase']
        prompt = (home / ('prompt-second.txt' if phase == 'second' else 'prompt-start.txt')).read_text()
        prompt += '\n\n这是启动或续跑。先查看 clock.py、tc.py inbox 及现有源码；保留已有进展。'
        result = subprocess.run(args, input=prompt, text=True)
        (state / 'exit.json').write_text(json.dumps({'returncode': result.returncode, 'time': time.time()}))
    finally:
        tunnel.stdin.close()
        try:
            tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel.terminate()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name', default='soccer-team')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('start').add_argument('--no-agent', action='store_true', help='仅创建与验证，不调用模型')
    for name in ('prepare', 'doctor', 'resume', 'stop', 'status', 'test', 'test-agent', 'rebuild-agent-bots', 'recreate'):
        sub.add_parser(name)
    phase = sub.add_parser('phase')
    phase.add_argument('phase', choices=['meeting', 'first', 'halftime', 'second', 'finished'])
    phase.add_argument('--minutes', type=float)
    work = sub.add_parser('worker')
    work.add_argument('--lock-fd', type=int, required=True)
    a = p.parse_args()
    import re
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{1,62}', a.name):
        p.error('name must be 2..63 letters, digits, periods or hyphens')
    home = ROOT / '.soccer-runs' / a.name
    if a.command == 'prepare':
        prepare(home)
        print(f'Mac 工作区已就绪：{home / "mac"}\n在该目录使用 ./dev check、./dev test、./dev chat。')
    elif a.command == 'start':
        start(a, home)
    elif a.command == 'doctor':
        doctor(a, home)
    elif a.command == 'resume':
        configure(a, home)
        doctor(a, home)
        launch(a, home)
    elif a.command == 'worker':
        worker(a, home)
    elif a.command == 'stop':
        run('sbx', 'stop', a.name)
    elif a.command == 'phase':
        state = home / 'state'
        if a.minutes is None:
            a.minutes = {'meeting': 5, 'first': 40, 'halftime': 0, 'second': 40, 'finished': 0}[a.phase]
        if a.phase in ('halftime', 'second', 'finished'):
            label = 'final' if a.phase == 'finished' else 'first'
            snapshot = state / 'submissions' / label
            if not snapshot.exists():
                for role in ('mac', 'agent'):
                    for filename in ('player.cpp', 'partner.cpp'):
                        source = home / role / 'submission' / filename
                        if source.exists():
                            if not source.resolve().is_relative_to((home / role).resolve()):
                                raise RuntimeError('submission symlink escapes workspace')
                            dest = snapshot / role / filename
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(source.read_bytes())
        temp = state / 'phase.tmp'
        temp.write_text(json.dumps({'phase': a.phase, 'deadline': time.time() + max(0, a.minutes) * 60}))
        temp.replace(state / 'phase.json')
        message = (HERE / f'{a.phase}.txt').read_text() if a.phase in ('first', 'second') else f'阶段切换：{a.phase}；停止修改 submission。'
        chat(state, 'host', 'send', {'message': message})
        print(f'阶段已切换：{a.phase}，{a.minutes} 分钟。若队友已结束本轮，运行 resume 继续。')
    elif a.command == 'status':
        print(json.dumps(chat(home / 'state', 'mac', 'clock', {}), ensure_ascii=False, indent=2))
        for filename in ('exit.json',):
            if (home / 'state' / filename).exists():
                print((home / 'state' / filename).read_text())
        run('sbx', 'ls')
    elif a.command == 'test':
        run(sys.executable, home / 'mac/dev.py', 'test')
    elif a.command == 'test-agent':
        run('sbx', 'exec', '-w', '/workspace', a.name, 'python3', 'match.py', 'run',
            '--me', '/teammate/submission/player.cpp', '--mate', 'submission/player.cpp', '--opp', 'baseline', '--seeds', '2')
    elif a.command == 'rebuild-agent-bots':
        ensure_agent_bots(a.name, force=True)
    elif a.command == 'recreate':
        state = home / 'state'
        with (state / 'worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('队友仍在运行，请先执行 stop，再 recreate。')
            backup = state / ('runtime-backup-' + str(time.time_ns()))
            backup.mkdir()
            run('sbx', 'cp', f'{a.name}:{SANDBOX_CODEX_HOME}', backup)
            run('sbx', 'rm', '-f', a.name)
            (state / 'created').unlink(missing_ok=True)
            a.no_agent = True
            start(a, home)
            print(f'已重建为 2 CPU / 4 GiB；工作区和队聊保留，旧 Codex 状态备份在 {backup}。未启动比赛。')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        sys.exit(1)
