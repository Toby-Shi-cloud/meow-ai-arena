#!/usr/bin/env python3
"""把 workspace/ 的选手包复制并移植到 unix_workspace/（macOS + Linux）。

用法：
    python3 build_unix_workspace.py

- unix_workspace/arena.cpp 是单独维护的 POSIX 移植版，本脚本不覆盖它。
- unix_workspace/.gitignore 也保留，其余生成文件都不纳入版本库。
- workspace/run.cmd 不再复制，改为生成 unix_workspace/run.sh。
- build.py、match.py 里的 .exe 去掉；编译参数改成 clang++/g++ + -pthread（macOS 另加 64 MB 栈）。
- 额外生成 run.sh 和 build_reference.py（输出无扩展名的 arena 和三个参考 bot）。
- README/RULES 的中英文描述从 Windows/AppContainer 改为 Unix 说法。
- 其他文件原样复制。

可以重复运行：每次都会按 workspace/ 的当前内容重新生成。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / 'workspace'
DST = ROOT / 'unix_workspace'

COPY_VERBATIM = {'template.cpp', 'soccer_engine.py', 'viewer.html', '示例回放.txt'}
PATCHED = {'build.py', 'match.py', 'replay.py', 'soccer.hpp',
           'README.md', 'README_en.md', 'RULES.md', 'RULES_en.md'}

RUN_SH = '''#!/bin/sh
# 找一个 Python 3.8 以上，在本目录运行给定的脚本。不带参数时显示 match.py 帮助。
# 用法：sh run.sh match.py check player.cpp
cd "$(dirname -- "$0")" || exit 1
ok='import sys; sys.exit(sys.version_info < (3, 8))'
run() {
    py=$1
    shift
    if [ "$#" -eq 0 ]; then
        exec "$py" -X utf8 match.py -h
    fi
    exec "$py" -X utf8 "$@"
}
for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1 && "$py" -c "$ok" >/dev/null 2>&1; then
        run "$py" "$@"
    fi
done
# PATH 里没有时，再看 macOS / Homebrew 的常见位置。
for py in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ -x "$py" ] && "$py" -c "$ok" >/dev/null 2>&1; then
        run "$py" "$@"
    fi
done
echo '没有找到 Python 3.8 以上的版本（试过 python3、python 和常见安装位置）。请把这个情况报告出来。' >&2
exit 1
'''

BUILD_REFERENCE = '''"""从公开源码编译三个参考 bot 和比赛裁判（Unix 版）。"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import build
import match

BOTS = {'baseline': 'plan16', 'midfield': 'plan32_b1.5', 'sparring': 'pro_b0_n128'}


def main():
    compiler = build.find_compiler()
    if not compiler:
        raise SystemExit('没有找到 C++ 编译器；请设置 CXX 或把编译器加入 PATH。')
    target = HERE / 'bots'
    target.mkdir(exist_ok=True)
    env = dict(os.environ)
    env['PATH'] = str(Path(compiler).resolve().parent) + os.pathsep + env.get('PATH', '')
    for name, variant in BOTS.items():
        subprocess.run([compiler, *build.FLAGS, f'-DBOT_NAME="{variant}"', 'bot_main.cpp',
                        '-o', str(target / name)],
                       cwd=ROOT / 'sparring', env=env, check=True, timeout=300)
        print(f'{name}: {variant}')
    print(match.arena_exe())


if __name__ == '__main__':
    main()
'''


def replace(text, old, new, where, count=None):
    """替换 old → new；count=None 表示至少一处、全部替换；否则必须恰好 count 处。"""
    found = text.count(old)
    if found == 0 or (count is not None and found != count):
        want = '至少 1' if count is None else str(count)
        raise SystemExit(f'{where}: 预期 {want} 处，实际 {found} 处：{old[:100]!r}')
    return text.replace(old, new)


def unix_python(text, where):
    """文档/脚本里的 python 命令改成 python3。"""
    return replace(text, 'python ', 'python3 ', where)


def patch_build_py(text):
    where = 'workspace/build.py'
    text = replace(text, 'python build.py', 'python3 build.py', where)
    text = replace(text, 'player.exe', 'player', where)          # 编译产物名
    text = replace(text, 'x.exe', 'x', where)                    # 文档里的自定义输出名
    text = replace(
        text,
        '  g++ -std=c++17 -O2 -static -Wl,--stack,67108864 <你的文件>.cpp -o player\n',
        '  clang++ -std=c++17 -O2 -pthread <你的文件>.cpp -o player\n'
        '  （macOS 另加 -Wl,-stack_size,0x4000000，保持和 Windows 版相同的 64 MB 栈）\n',
        where)
    text = replace(
        text,
        '- 编译器找法：环境变量 CXX → PATH 里的 g++ → PATH 里的 clang++ → 常见的 w64devkit 位置。',
        '- 编译器找法：环境变量 CXX → PATH 里的 g++ → PATH 里的 clang++ → /usr/bin 等常见位置。',
        where)
    text = replace(
        text,
        "FLAGS = ['-std=c++17', '-O2', '-static', '-Wl,--stack,67108864']",
        "FLAGS = ['-std=c++17', '-O2', '-pthread']\n"
        "if sys.platform == 'darwin':\n"
        "    FLAGS.append('-Wl,-stack_size,0x4000000')  # 与 Windows 版相同的 64 MB 栈",
        where)
    text = replace(
        text,
        """    for base in KIT.parents:
        guess = base / 'tools' / 'w64devkit' / 'bin' / 'g++.exe'
        if guess.is_file():
            return str(guess)
    for guess in (Path('C:/w64devkit/bin/g++.exe'), Path('C:/msys64/ucrt64/bin/g++.exe'), Path('C:/msys64/mingw64/bin/g++.exe')):
        if guess.is_file():
            return str(guess)
""",
        """    for guess in ('/usr/bin/clang++', '/usr/bin/g++', '/opt/homebrew/bin/g++', '/usr/local/bin/g++'):
        if Path(guess).is_file():
            return guess
""",
        where)
    text = replace(text, "BUILD / f'{src.stem}-{key}.exe'", "BUILD / f'{src.stem}-{key}'", where)
    text = replace(text, "Path(a.src).with_suffix('.exe')", "Path(a.src).with_suffix('')", where)
    text = replace(text, "shutil.copyfile(cached, out)", "shutil.copy2(cached, out)", where)
    text = replace(text, "print('正式编译命令：', 'g++ ' + ' '.join(FLAGS)",
                   "print('正式编译命令：', 'clang++ ' + ' '.join(FLAGS)", where)
    text = replace(text, '引擎\\arena_api.py', '引擎/arena_api.py', where)
    text = replace(
        text,
        '  这只是辅助检查；真正的限制是运行时的沙箱（不能开子进程、不能读写文件，见 RULES.md 第 8、9 节）。',
        '  这只是辅助检查；Unix 版裁判不内置沙箱，不能开子进程、不能读写文件等限制由外部运行环境提供'
        '（见 RULES.md 第 8、9 节）。',
        where)
    return text


def patch_match_py(text):
    where = 'workspace/match.py'
    text = replace(text, '对局由 arena.exe（与正式比赛同一个裁判）完成。',
                   '对局由 arena（与正式比赛同一个裁判）完成。', where)
    text = replace(text, '  路径.exe        直接用', '  可执行文件路径  直接用', where)
    text = replace(text, "ARENA = KIT / 'arena.exe'", "ARENA = KIT / 'arena'", where)
    text = replace(
        text,
        "BUILTIN = {'baseline': KIT / 'bots' / 'baseline.exe', 'midfield': KIT / 'bots' / 'midfield.exe', "
        "'sparring': KIT / 'bots' / 'sparring.exe'}",
        "BUILTIN = {'baseline': KIT / 'bots' / 'baseline', 'midfield': KIT / 'bots' / 'midfield', "
        "'sparring': KIT / 'bots' / 'sparring'}",
        where)
    text = replace(text, '# 没有现成的 arena.exe：从源码编译（需要 g++）',
                   '# 没有现成的 arena：从源码编译（需要 C++ 编译器）', where)
    text = replace(text, "raise build.BuildError('没有 arena.exe，也没有找到 g++ 来编译它')",
                   "raise build.BuildError('没有 arena，也没有找到 C++ 编译器来编译它')", where)
    text = replace(
        text,
        "    r = subprocess.run([cxx, '-std=c++17', '-O2', '-static', 'arena.cpp', '-o', 'arena.exe', "
        "'-lshell32', '-luserenv'], cwd=KIT,\n",
        "    flags = ['-std=c++17', '-O2', '-pthread']\n"
        "    if sys.platform == 'darwin':\n"
        "        flags.append('-Wl,-stack_size,0x4000000')\n"
        "    r = subprocess.run([cxx, *flags, 'arena.cpp', '-o', 'arena'], cwd=KIT,\n",
        where)
    text = replace(text, r'\\g0_p<球员号>.txt', '/g0_p<球员号>.txt', where)
    text = replace(text, r'rdir}\\g<局号>.txt', 'rdir}/g<局号>.txt', where)
    text = replace(text, '# 开源版源码更新后重建，避免继续运行带旧沙箱路径的缓存裁判。',
                   '# 开源版源码更新后重建，避免继续运行旧缓存的裁判。', where)
    text = replace(text, '"""参赛者 → (显示名, exe 路径)"""',
                   '"""参赛者 → (显示名, 可执行文件路径)"""', where)
    text = replace(text, '"""entries: [(名字, exe)]；', '"""entries: [(名字, 可执行文件)]；', where)
    text = unix_python(text, where)
    return text


def patch_replay_py(text):
    where = 'workspace/replay.py'
    text = replace(text, 'python replay.py', 'python3 replay.py', where)
    text = replace(text, "f'python {me}", "f'python3 {me}", where)
    text = replace(text, 'f"下一步：python {me}', 'f"下一步：python3 {me}', where)
    return text


def patch_soccer_hpp(text):
    where = 'workspace/soccer.hpp'
    return replace(text, '裁判程序 arena.exe 和选手程序', '裁判程序 arena 和选手程序', where)


def patch_readme_zh(text):
    where = 'workspace/README.md'
    text = replace(
        text,
        '# 2v2 足球 · 选手包\n\n',
        '# 2v2 足球 · 选手包（Unix 版）\n\n'
        '> 本目录由 `../build_unix_workspace.py` 从 `../workspace/` 生成；`arena.cpp` 是 POSIX 移植版。'
        'Unix 版裁判不内置沙箱，正式比赛的进程 / 文件 / 网络限制由外部运行环境提供。\n\n',
        where)
    text = replace(text, '| `arena.exe`、`arena.cpp` | 裁判程序（与正式比赛相同）及源码 |',
                   '| `arena`、`arena.cpp` | 裁判程序（与正式比赛同一份逻辑）及源码；Unix 版不内置沙箱 |', where)
    for name in ('baseline', 'midfield', 'sparring'):
        text = replace(text, f'| `bots/{name}.exe` |', f'| `bots/{name}` |', where)
    text = replace(
        text,
        '| `run.cmd` | 找到 Python 并运行脚本：`.\\run.cmd match.py ...`（直接 `python match.py ...` 也行） |',
        '| `run.sh` | 找到 Python 并运行脚本：`sh run.sh match.py ...`（直接 `python3 match.py ...` 也行） |',
        where)
    text = replace(
        text,
        '在本目录运行（需要 Python 3.8+ 和 g++；g++ 不在 PATH 时设环境变量 `CXX`）：',
        '在本目录运行（需要 macOS / Linux、Python 3.8+ 和 clang++ 或 g++；编译器不在 PATH 时设环境变量 `CXX`）：',
        where)
    text = replace(text, r'..\队友目录\player.cpp', '../队友目录/player.cpp', where)
    text = replace(text, r'replays\<回放>.txt', 'replays/<回放>.txt', where)
    text = replace(
        text,
        '运行后会自动生成 `build/`（编译缓存）和 `replays/`（回放）。',
        '第一次使用先运行 `python3 build_reference.py`，编译裁判和三个参考 bot（源码在 `../sparring/`）。'
        '运行后会自动生成 `build/`（编译缓存）和 `replays/`（回放）。',
        where)
    text = replace(text, '`.cpp` 路径（自动编译）、`.exe` 路径。',
                   '`.cpp` 路径（自动编译）、可执行文件路径。', where)
    marker = '- 你的程序在沙箱里运行'
    if marker not in text:
        raise SystemExit(f'{where}: 找不到沙箱提示段：{marker!r}')
    text = text[:text.index(marker)] + (
        '- 正式比赛中你的程序在沙箱里运行（不能开子进程、不能读写文件，见 `RULES.md` 第 8 节）。'
        'Unix 版裁判不内置沙箱，这些限制由外部运行环境提供；本地 `match.py` 直接运行你的程序。'
        '`build.py` 编译前会做源码检查（不能包含系统 / 进程类头文件，见 `RULES.md` 第 9 节）。\n')
    return unix_python(text, where)


def patch_readme_en(text):
    where = 'workspace/README_en.md'
    text = replace(
        text,
        '# 2v2 soccer · base contestant package\n\n',
        '# 2v2 soccer · base contestant package (Unix)\n\n'
        '> Generated from ../workspace/ by ../build_unix_workspace.py; arena.cpp is the POSIX port. '
        'The Unix referee has no built-in sandbox; official process, file, and network restrictions '
        'are supplied by the external runner.\n\n',
        where)
    text = replace(text, '| arena.cpp | Referee source; arena.exe is built locally |',
                   '| arena.cpp | Referee source; arena is built locally |', where)
    text = replace(text, '| run.cmd | Finds Python and runs a script on Windows |',
                   '| run.sh | Finds Python and runs a script on macOS / Linux |', where)
    old_req = ('Requires Windows, Python 3.10+ and g++. The AppContainer referee does not run on Linux/macOS. '
               'Put g++ on PATH or set CXX; the archive also searches repository tools/w64devkit/bin. '
               'From the parent directory first run `python build_reference.py` to compile the referee and '
               'baseline/midfield/sparring bots. Their source is public here but was not given to contestants. '
               'Source adaptations are recorded in [source_manifest.json](../source_manifest.json).')
    new_req = ('Requires macOS or Linux, Python 3.10+ and clang++ or g++. The Unix referee keeps the same '
               'process and CPU-timing model but has no built-in sandbox; the runtime restrictions are '
               'supplied by the external environment. Put the compiler on PATH or set CXX. '
               'From this folder first run `python3 build_reference.py` to compile the referee and '
               'baseline/midfield/sparring bots. Their source is public here but was not given to '
               'contestants. Source adaptations are recorded in '
               '[source_manifest.json](../source_manifest.json).')
    text = replace(text, old_req, new_req, where)
    old_run = ('Generated build/ and replays/ folders are local artifacts. Diagnostics go to replay-adjacent '
               'stderr files. CPU budget is 10 ms per turn; busy machines may affect measurements. Local '
               'matches use the same sandbox, forbidding child processes and file I/O. If antivirus prevents '
               'sandbox startup, set SOCCER_BOX_ROOT to a folder where new executables can run. Without '
               'SOCCER_BOX_ROOT, the open-source referee uses only _soccer_box/ beside its executable and '
               'stops if that directory cannot run the probe. With SOCCER_BOX_ROOT, it uses only the specified '
               'directory. There is no fixed machine-directory fallback.')
    new_run = ('Generated build/ and replays/ folders are local artifacts. Diagnostics go to replay-adjacent '
               'stderr files. CPU budget is 10 ms per turn; busy machines may affect measurements. The Unix '
               'referee starts each program directly and has no built-in sandbox; process, file, and network '
               'limits are supplied by the external runner.')
    text = replace(text, old_run, new_run, where)
    text = unix_python(text, where)
    return text


def patch_rules_zh(text):
    where = 'workspace/RULES.md'
    text = replace(
        text,
        '**运行环境限制（沙箱）**：每个程序在独立的 Windows AppContainer 沙箱里运行，只能用标准输入输出和裁判交流：',
        '**运行环境限制（沙箱）**：正式比赛中每个程序在独立的沙箱里运行，只能用标准输入输出和裁判交流：',
        where)
    text = replace(text, '（每个程序同时只能有 1 个进程；`system()`、`CreateProcess` 等都会失败）。',
                   '（每个程序同时只能有 1 个进程；`system()`、`fork()`、`exec()` 等都会失败）。', where)
    text = replace(
        text,
        '- 选手包里的 `match.py` 用的是同一个 `arena.exe`，本地对战也在这个沙箱里跑，限制和正式比赛相同。',
        '- 选手包里的 `match.py` 用的是同一份裁判程序 `arena`。Unix 版 `arena` 不内置沙箱，本地对战直接运行程序；'
        '正式比赛的沙箱由外部运行环境提供。',
        where)
    text = replace(
        text,
        '  - 不能包含系统 / 进程类头文件：`windows.h`、`winsock2.h` 等以 `win` 开头的，`processthreadsapi.h` 等以 `api.h` 结尾的，'
        '`unistd.h`、`process.h`、`io.h`、`direct.h`、`fcntl.h`、`tlhelp32.h`、`sys/…` 等；',
        '  - 不能包含系统 / 进程类头文件：`unistd.h`、`process.h`、`io.h`、`direct.h`、`fcntl.h`、`sys/…` 等，'
        '以及 `windows.h`、`winsock2.h` 等 Windows 系统头文件；',
        where)
    text = replace(
        text,
        '- 编译命令（Windows，g++ / w64devkit）：`g++ -std=c++17 -O2 -static -Wl,--stack,67108864 player.cpp -o player.exe`'
        '（同目录放 `soccer.hpp`）。',
        '- 编译命令（macOS / Linux，clang++ 或 g++）：`clang++ -std=c++17 -O2 -pthread player.cpp -o player`'
        '（macOS 另加 `-Wl,-stack_size,0x4000000`；同目录放 `soccer.hpp`）。',
        where)
    text = replace(text, '- 选手包里的 `build.py` 用同样的命令编译；`match.py` 用正式裁判 `arena.exe` 打本地比赛；',
                   '- 选手包里的 `build.py` 用同样的命令编译；`match.py` 用裁判 `arena` 打本地比赛；', where)
    text = replace(text, '  - 这道检查只是提前提醒；真正的限制是上面第 8 节的运行沙箱。',
                   '  - 这道检查只是提前提醒；正式比赛的运行沙箱见第 8 节（Unix 版由外部运行环境提供）。', where)
    return text


def patch_rules_en(text):
    where = 'workspace/RULES_en.md'
    old = ('Each bot runs in its own Windows AppContainer. It cannot create child processes, read or write '
           'files, access the network or other processes; only minimal environment variables are exposed. '
           'The working directory is read-only. Failed forbidden operations return errors rather than '
           'automatically forfeiting the game. Local match.py uses this same sandbox.')
    new = ('In official games each bot runs in its own sandbox. It cannot create child processes, read or '
           'write files, access the network or other processes; only minimal environment variables are '
           'exposed. The working directory is read-only. Failed forbidden operations return errors rather '
           'than automatically forfeiting the game. The Unix referee has no built-in sandbox; local match.py '
           'starts the program directly, and the official restrictions are supplied by the external runner.')
    text = replace(text, old, new, where)
    text = replace(text, 'Original Windows command (g++ / w64devkit, with soccer.hpp alongside):',
                   'Unix command (clang++ or g++, with soccer.hpp alongside; add '
                   '-Wl,-stack_size,0x4000000 on macOS):', where)
    text = replace(text, 'g++ -std=c++17 -O2 -static -Wl,--stack,67108864 player.cpp -o player.exe',
                   'clang++ -std=c++17 -O2 -pthread player.cpp -o player', where)
    text = replace(text, 'The precheck is advisory protection; the runtime sandbox enforces isolation.',
                   'The precheck is advisory protection; the official runtime sandbox enforces isolation '
                   '(the Unix referee relies on the external runner).', where)
    return text


def patch_text(name, text):
    if name == 'build.py':
        return patch_build_py(text)
    if name == 'match.py':
        return patch_match_py(text)
    if name == 'replay.py':
        return patch_replay_py(text)
    if name == 'soccer.hpp':
        return patch_soccer_hpp(text)
    if name == 'README.md':
        return patch_readme_zh(text)
    if name == 'README_en.md':
        return patch_readme_en(text)
    if name == 'RULES.md':
        return patch_rules_zh(text)
    if name == 'RULES_en.md':
        return patch_rules_en(text)
    raise AssertionError(name)


def write_text(path, text):
    with path.open('w', encoding='utf-8', newline='\n') as f:
        f.write(text)


def main():
    if not SRC.is_dir():
        raise SystemExit(f'找不到源选手包：{SRC}')
    DST.mkdir(exist_ok=True)
    for keep in ('arena.cpp', '.gitignore'):
        if not (DST / keep).is_file():
            raise SystemExit(f'unix_workspace/{keep} 不存在；请先把它放进仓库。')

    written = []
    for src in sorted(SRC.iterdir()):
        if not src.is_file() or src.name in ('arena.cpp', 'run.cmd'):
            continue
        dst = DST / src.name
        if src.name in COPY_VERBATIM or src.name not in PATCHED:
            dst.write_bytes(src.read_bytes())
        else:
            text = src.read_text(encoding='utf-8')
            write_text(dst, patch_text(src.name, text))
        written.append(dst.name)

    run_sh = DST / 'run.sh'
    write_text(run_sh, RUN_SH)
    run_sh.chmod(0o755)
    written.append('run.sh')

    write_text(DST / 'build_reference.py', BUILD_REFERENCE)
    written.append('build_reference.py')

    print(f'已生成 {DST}：')
    for name in written:
        print(f'  {name}')
    print('保留不动的文件：arena.cpp、.gitignore')


if __name__ == '__main__':
    main()
