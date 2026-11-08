#!/usr/bin/env python3
"""Script-only teammate messaging. Identity is fixed by the host relay."""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path


def request(action, **values):
    config = json.loads((Path(__file__).parent / 'team.json').read_text())
    if config['role'] == 'mac':
        sys.path.insert(0, config['controller'])
        from team import chat
        return chat(Path(config['state']), 'mac', action, values)
    req = urllib.request.Request('http://localhost:2345/teamchat',
        data=json.dumps({'action': action, **values}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=65) as r:
        return json.load(r)


def main():
    p = argparse.ArgumentParser()
    s = p.add_subparsers(dest='cmd', required=True)
    send = s.add_parser('send')
    send.add_argument('message', nargs='?')
    send.add_argument('-f', '--file')
    s.add_parser('inbox')
    wait = s.add_parser('wait')
    wait.add_argument('seconds', type=int, nargs='?', default=60)
    log = s.add_parser('log')
    log.add_argument('--last', type=int, default=20)
    log.add_argument('--all', action='store_true')
    s.add_parser('status')
    a = p.parse_args()
    if a.cmd == 'send':
        message = Path(a.file).read_text() if a.file else sys.stdin.read() if a.message == '-' else a.message
        if not message:
            p.error('请提供消息或 -f 文件')
        result = request('send', message=message)
    elif a.cmd == 'wait':
        deadline = time.monotonic() + max(0, min(a.seconds, 600))
        while True:
            result = request('inbox')
            if result or time.monotonic() >= deadline:
                break
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    else:
        result = request(a.cmd, last=0 if getattr(a, 'all', False) else getattr(a, 'last', 20))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
