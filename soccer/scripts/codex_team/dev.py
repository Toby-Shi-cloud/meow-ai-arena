#!/usr/bin/env python3
"""Human-friendly native Mac development commands; no sandbox required for tests."""
import argparse
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('build', 'check'):
        p = commands.add_parser(name)
        p.add_argument('source', nargs='?', default='submission/player.cpp')
    test = commands.add_parser('test', help='Mac 原生联合对局，自动使用队友最新源码')
    test.add_argument('--opp', default='baseline', choices=['baseline', 'midfield', 'sparring'])
    test.add_argument('--seeds', type=int, default=2)
    test.add_argument('--workers', type=int, default=1)
    test.add_argument('--me', default='submission/player.cpp')
    test.add_argument('--mate', default='../agent/submission/player.cpp')
    for name in ('chat', 'replay'):
        p = commands.add_parser(name)
        p.add_argument('args', nargs=argparse.REMAINDER)
    commands.add_parser('clock')
    commands.add_parser('viewer')
    a = parser.parse_args()
    if a.command == 'build':
        args = [sys.executable, 'build.py', a.source]
    elif a.command == 'check':
        args = [sys.executable, 'match.py', 'check', a.source]
    elif a.command == 'test':
        args = [sys.executable, 'match.py', 'run', '--me', a.me, '--mate', a.mate,
                '--opp', a.opp, '--seeds', str(a.seeds), '--workers', str(a.workers)]
    elif a.command in ('chat', 'replay'):
        args = [sys.executable, 'tc.py' if a.command == 'chat' else 'replay.py', *a.args]
    elif a.command == 'clock':
        args = [sys.executable, 'clock.py']
    else:
        args = ['open', 'viewer.html']
    return subprocess.run(args, cwd=HERE).returncode


if __name__ == '__main__':
    sys.exit(main())
