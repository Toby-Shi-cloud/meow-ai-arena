"""Build the human workspace's referee and bots for the host, never for Linux."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

BOTS = {'baseline': 'plan16', 'midfield': 'plan32_b1.5', 'sparring': 'pro_b0_n128'}


def prepare_native(root, workspace):
    if sys.platform != 'darwin':
        raise RuntimeError('人类工作区需要在 macOS 上初始化。')
    compiler = shutil.which(os.environ.get('CXX', 'clang++'))
    if not compiler:
        raise RuntimeError('没有找到 C++ 编译器，请先安装 Xcode Command Line Tools。')
    flags = ['-std=c++17', '-O2', '-pthread', '-Wl,-stack_size,0x4000000']
    sources = {
        'arena.cpp': (workspace / 'arena.cpp').read_bytes(),
        'soccer.hpp': (workspace / 'soccer.hpp').read_bytes(),
        'bot_main.cpp': (root / 'sparring/bot_main.cpp').read_bytes(),
        'bots.hpp': (root / 'sparring/bots.hpp').read_bytes().replace(
            b'"../workspace/soccer.hpp"', b'"soccer.hpp"'),
    }
    version = subprocess.check_output([compiler, '--version'])
    digest = hashlib.sha256(version + platform.machine().encode() + repr(flags).encode()
                            + repr(BOTS).encode() + b''.join(sources.values())).hexdigest()
    manifest = workspace / '.native-build.json'
    binaries = [workspace / 'arena', *(workspace / 'bots' / name for name in BOTS)]
    def fingerprint(path):
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    expected = {str(p.relative_to(workspace)): fingerprint(p) for p in binaries}
    if manifest.exists() and all(expected.values()):
        if json.loads(manifest.read_text()) == {'inputs': digest, 'binaries': expected}:
            return
    print('构建 Mac 原生裁判和参考 bot（保留已有 submission）…', flush=True)
    (workspace / 'bots').mkdir(exist_ok=True)
    # Temporary source files never enter either participant's workspace.
    with tempfile.TemporaryDirectory(prefix='soccer-native-') as temporary:
        build = Path(temporary)
        for name, data in sources.items():
            (build / name).write_bytes(data)
        targets = [('arena', 'arena.cpp', [])] + [
            (name, 'bot_main.cpp', [f'-DBOT_NAME="{variant}"']) for name, variant in BOTS.items()]
        for name, source, defines in targets:
            subprocess.run([compiler, *flags, *defines, source, '-o', name],
                           cwd=build, check=True, timeout=300)
        for binary in binaries:
            staged = binary.with_name(binary.name + '.native-tmp')
            shutil.copy2(build / binary.name, staged)
            staged.replace(binary)
    manifest.write_text(json.dumps({'inputs': digest, 'binaries': {
        str(p.relative_to(workspace)): fingerprint(p) for p in binaries}}, indent=2))
