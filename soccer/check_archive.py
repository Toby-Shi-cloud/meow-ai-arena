"""检查相对链接、归档哈希、赛程、上场源码与逐局积分的一致性。"""
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'tournament'))
from score import standings


def read(path):
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if path.suffix == '.gz' else raw)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    count = 0
    docs = [*ROOT.rglob('*.md'), ROOT.parent / 'README.md', ROOT.parent / 'README_en.md']
    for p in docs:
        if any(s in p.relative_to(ROOT.parent).parts for s in ['out', 'build', '_soccer_box', '.soccer-runs', 'unix_workspace']):
            continue
        text = p.read_text(encoding='utf-8-sig')
        for target in re.findall(r'!?\[[^\]\n]*\]\(([^\n)]+)\)', text):
            target = target.strip().split(' "', 1)[0].strip('<>')
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            dest = (p.parent / unquote(parsed.path)).resolve()
            require(dest.exists(), f'链接不存在：{p.relative_to(ROOT.parent)} -> {target}')
            count += 1
    manifest = read(ROOT / 'source_manifest.json')
    for rel, info in manifest.items():
        p = ROOT / rel
        require(p.is_file(), f'缺少归档文件：{rel}')
        require(hashlib.sha256(p.read_bytes()).hexdigest() == info['sha256'], f'哈希不符：{rel}')
        if not info['modified']:
            require(info['original_sha256'] == info['sha256'], f'原样声明不符：{rel}')
    pack = read(ROOT / 'packages' / 'player_pack_manifest.json')
    slots = {r['seat'] for r in read(ROOT / 'bots' / 'roster.json')}
    require(set(pack['slots']) == slots and len(pack['slots']) == 12, '赛时包席位清单不全')
    paths = [r['path'] for r in pack['player_files']]
    require(len(set(paths)) == len(paths) == pack['files_per_slot'] == 23, '初始包文件清单不全或重复')
    require({p['team'] for p in pack['half_time_packages']} == {f'team{i}' for i in range(1, 7)}
            and len(pack['half_time_packages']) == 6, '中场包队伍清单不全或重复')
    records = pack['player_files'] + [f for p in pack['half_time_packages'] for f in p['fixed_files']]
    for record in records:
        if 'historical_sha256_by_seat' in record:
            require(set(record['historical_sha256_by_seat']) == slots, '逐席位历史哈希不全')
        if 'published_source' not in record:
            continue
        src = (ROOT / record['published_source']).resolve()
        require(src.is_relative_to(ROOT) and src.is_file(), '赛时包公开来源不存在或越界')
        require(hashlib.sha256(src.read_bytes()).hexdigest() == record['published_sha256'],
                f'赛时包公开来源哈希不符：{record["published_source"]}')
    clocks = read(ROOT / 'packages' / 'timing.json')['records']
    require(len(clocks) == 12 and {r['seat'] for r in clocks} == slots, '有效用时席位不全或重复')
    for row in clocks:
        for phase in ['half1', 'half2']:
            t = row[phase]
            require(abs(t['elapsed_seconds'] - t['compensation_seconds'] - t['net_seconds']) < .001,
                    f'净时长计算不符：{row["seat"]}/{phase}')
    games_by_phase = {}
    total = 0
    for phase, expected in [('halftime', 2112), ('final', 2904), ('individual', 3264)]:
        data = read(ROOT / 'results' / phase / 'results.json.gz')
        schedule = read(ROOT / 'tournament' / (phase + '.json.gz'))
        games = {g['id']: g for g in data['games']}
        require(len(games) == len(data['games']) == expected, f'{phase} 局数或去重失败')
        require(set(games) == {g['id'] for g in schedule['games']}, f'{phase} 赛程局号不符')
        for planned in schedule['games']:
            g = games[planned['id']]
            for key in ['A', 'B', 'seed', 'k', 'first_kick', 'order']:
                require(g[key] == planned[key], f'{phase}/{g["id"]} {key} 不符')
            require(g['seed'] == data['seeds'][g['k']], '种子序号映射错误')
            require(g['order'] == (['b', 'a'] if g['k'] % 2 else ['a', 'b']), '座位轮换错误')
            seats = [side + '/' + slot for side in [g['A'], g['B']] for slot in g['order']]
            require(g['seats'] == seats, f'{phase}/{g["id"]} 实际座位不符')
            require(not any(v.get('status') == 'start_fail' for v in g['faults'].values()), '发现启动失败局')
        recalculated = standings(data)
        for actual, stored in zip(recalculated, data['standings']):
            for key in ['team', 'rank', 'pts', 'w', 'd', 'l', 'gf', 'ga', 'gd']:
                require(actual[key] == stored[key], f'{phase} {key} 复算不符')
        require(read(ROOT / 'results' / phase / 'programs.json') == data['programs'], '程序索引不符')
        for seat, program in data['programs'].items():
            src = ROOT / program['src']
            require(src.is_file(), f'源码不存在：{program["src"]}')
            require(hashlib.sha256(src.read_bytes()).hexdigest() == program['src_sha256'], f'{phase}/{seat} 上场源码哈希不符')
            require(schedule['programs'][seat] == program['sha256'], f'{phase}/{seat} 冻结可执行文件哈希不符')
        games_by_phase[phase] = games
        total += len(games)
    for package in pack['half_time_packages']:
        team = package['team']
        count_games = sum(team in (g['A'], g['B']) for g in games_by_phase['halftime'].values())
        require(count_games == package['replay_count'] == 512, f'{team} 中场回放清单局数不符')
    for selected in read(ROOT / 'replays' / 'selection.json'):
        g = games_by_phase[selected['phase']][selected['id']]
        require(selected['score'] == g['score'] and selected['seats'] == g['seats'], '回放清单不符')
        require((ROOT / selected['file']).is_file(), '缺少抽样回放')
    print(f'通过：{count} 个相对链接，{len(manifest)} 个归档哈希，{total} 局赛程与积分，全部上场源码和 18 局抽样回放索引；12 席位赛时包、6 个中场包及有效用时清单。')


if __name__ == '__main__':
    main()
