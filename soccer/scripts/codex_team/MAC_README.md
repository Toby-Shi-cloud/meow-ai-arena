# 人类开发工作区 · macOS 原生

直接用编辑器打开本目录。你的源码在 `submission/player.cpp`；下半场的个人队搭档为 `submission/partner.cpp`。
`arena` 和 `bots/` 已编译成当前 Mac 的原生程序。开发、编译、对局、回放均在 Mac 本地完成，测试不依赖沙箱启动。

## 日常操作

在本目录运行：

```sh
./dev build                       # 编译自己的 player.cpp
./dev check                       # 两局合法性、崩溃和耗时检查
./dev test                        # 自己 + 队友最新源码，对 baseline，4 局
./dev test --opp midfield --seeds 8
./dev test --opp sparring --seeds 8
./dev test --mate submission/partner.cpp --opp sparring  # 测试个人队
./dev chat send '我已完成回防逻辑，准备测试配合'
./dev chat inbox
./dev chat wait 60
./dev clock
./dev viewer                      # 打开回放查看器，拖入 replays/ 中的回放
./dev replay info replays/<回放>.txt
```

直接使用 `python3 build.py`、`python3 match.py`、`python3 replay.py` 同样可用。
`./dev test` 默认单进程跑比赛，减少 CPU 争抢；需要加速可设 `--workers 4`。

## 双方如何协作

- 你修改 `submission/`；agent 修改其独立 Linux 工作区的 `submission/`。
- agent 能从 `/teammate/submission/` 实时只读访问你的代码。你可查看 `../agent/submission/`，但不要编辑队友文件。
- Mac 测试读取队友的 `.cpp`，在本地 `build/` 缓存中编译成 Mac 程序，不运行或覆盖队友的 Linux 二进制。agent 在 Linux 中独立编译你的 `.cpp`。
- Mac 和 Linux 各有独立的编译缓存、裁判、参考 bot 与回放；不要互拷可执行文件。
- 队聊只用 `./dev chat`；Mac 可以随时发消息，agent 运行时通过自己的 `tc.py` 收取和回复。

比赛规则见 `RULES.md`，工具详解见 `TOOLS.md`。每步 CPU 限制相同，但 Mac 与 Linux 的耗时可能不同；双方都应留出性能余量。

## 计时与开赛

启动脚本会开启 agent 的战术会议；本目录的 `./dev clock` 查看共享阶段和剩余时间。
从仓库根目录运行控制命令：

```sh
python3 scripts/codex_team/team.py phase first     # 会议结束，上半场 40 分钟
python3 scripts/codex_team/team.py phase halftime  # 收卷，进入中场
python3 scripts/codex_team/team.py phase second    # 下半场 40 分钟
python3 scripts/codex_team/team.py resume          # agent 已结束上一轮时续跑
python3 scripts/codex_team/team.py phase finished  # 收卷，结束比赛
```

初始化或修复本工作区用仓库根目录的 `python3 scripts/codex_team/team.py prepare`。
这会保留已有提交并按需重建 Mac 原生程序，不启动模型、不开始比赛。
