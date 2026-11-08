# Mac + Codex 沙箱足球协作

在仓库根目录运行：

```sh
./start_codex_team.sh
```

需要已登录、能工作的本机 `sbx`，Python 3.9+、Xcode Command Line Tools（`clang++`），以及 Mac 上的 `http://localhost:3425/v1` 模型服务。启动使用 `sbx create codex`，首次自动构建 GCC 工具链 kit（构建器需要下载 Debian 和软件包，比赛沙箱始终禁止出网）。本次已在 sbx v0.51.0 / Apple Silicon 验证。agent 使用现有 Linux aarch64 bot；人类工作区的裁判和三个 bot 会自动从源码编译成当前 Mac 的 Mach-O 程序。若缺少 bot 或 ELF 架构不匹配，会在目标沙箱内自动从源码重建。

模型固定为 `codex/gpt-6-astra`，effort 为 `high`，真实网关密钥从 `SOCCER_GATEWAY_API_KEY` 环境变量读取，或读取 `.soccer-runs/gateway.json` 的 `api_key`（已配置，文件不纳入版本控制）。密钥仅由 Mac 中继使用；沙箱内使用占位密钥 `*`，base URL 为 `http://localhost:2345/v1`。配置放在独立的 `/home/agent/.soccer-codex/config.toml`，不修改 Mac 的 Codex 设置。

沙箱固定以 `--cpus 2 --memory 4g` 创建，CPU 和内存上限作用于整个沙箱（包含 agent、编译与比赛进程）。启动检查会拒绝超出上限的旧沙箱；执行 `python3 scripts/codex_team/team.py recreate` 可先备份 Codex 状态，再按限制重建，保留双方源码和队聊，不自动开赛。

默认启动后台 `codex exec`，自动从标准输入送入开赛提示词；没有交互终端。`start` 重复运行会复用已创建的沙箱；已运行的队友会拒绝重复启动。只创建和检查、不调用模型用：

```sh
python3 scripts/codex_team/team.py start --no-agent
```

## 工作位置与通信

| 用途 | Mac 上的路径 |
|---|---|
| 你编写的球员 | `.soccer-runs/soccer-team/mac/submission/player.cpp` |
| 队友的球员 | `.soccer-runs/soccer-team/agent/submission/player.cpp` |
| 你的队聊脚本 | `.soccer-runs/soccer-team/mac/tc.py` |
| 队友运行日志 | `.soccer-runs/soccer-team/state/worker.log` |
| 队友最终回复 | `.soccer-runs/soccer-team/agent/last-response.txt` |
| Mac 本地回放 | `.soccer-runs/soccer-team/mac/replays/` |
| agent 回放 | `.soccer-runs/soccer-team/agent/replays/` |
| 上半场、终场收卷 | `.soccer-runs/soccer-team/state/submissions/` |

原有 `unix_workspace/` 和 bot 不会被修改。双方各有独立且可直接使用的环境：人类在 Mac 原生编译和测试，agent 在 Linux 沙箱自主编译和测试，各自拥有裁判、三个参考 bot、编译缓存和回放。双方通过队聊脚本沟通，测试时读取队友源码，在自己的平台编译。你可以查看队友源码，但不要修改它。

人类开发入口（详细用法见 [Mac 工作区说明](MAC_README.md)）：

```sh
# 只准备或修复工作区，不启动 agent
python3 scripts/codex_team/team.py prepare
cd .soccer-runs/soccer-team/mac
# 用编辑器打开此目录，修改 submission/player.cpp
./dev build
./dev check
./dev test --opp sparring --seeds 8
./dev chat send '我的回防版本已通过测试'
./dev chat wait 60
./dev viewer
```

`./dev test` 默认读取 `../agent/submission/player.cpp`，将两人的源码都编译为 Mac 程序，不依赖运行中的沙箱。普通的 `python3 match.py` 也可直接在 Mac 工作区使用。重复准备会保留已有 submission，原生工具链仅在源码、编译器或产物变化时重建。

```sh
python3 .soccer-runs/soccer-team/mac/tc.py send '我负责接应和回防，你负责抢球；讨论一下 MSG 协议。'
python3 .soccer-runs/soccer-team/mac/tc.py inbox
python3 .soccer-runs/soccer-team/mac/tc.py wait 60
python3 .soccer-runs/soccer-team/mac/tc.py log --all
python3 .soccer-runs/soccer-team/mac/clock.py
```

`send -f 文件` 和 `send -` 支持长消息。队聊数据库只在 Mac 上，沙箱无法修改历史或冒充 Mac 身份。队友只在运行中轮询消息；消息不会强行中断它当前的模型调用。后台队友结束后，消息仍保存，`resume` 后会读取。

## 原有三阶段流程

提示词完全按沙箱视角编写，可直接复制，不包含宿主实现细节：

1. [开赛提示词](prompt.txt)：战术会议 **5 分钟**，随后上半场 **40 分钟**。启动脚本自动发送。
2. [允许开始编码](first.txt)：会议结束时由主办方发送。
3. [下半场提示词](second.txt)：下半场 **40 分钟**；新增个人队 `partner.cpp`，团队分、个人分各占 50%。

阶段由你通过脚本明确切换；计时归零不会擅自跳过中场复盘。`clock.py` 提供计时，提示词要求到点停止修改。命令从仓库根目录执行：

```sh
# 5 分钟会议结束后：发送“可以开始写代码”，开始上半场
python3 scripts/codex_team/team.py phase first --minutes 40

# 上半场结束：收卷双方源码，进入中场
python3 scripts/codex_team/team.py phase halftime --minutes 0

# 你可运行联合测试并通过 tc.py 分享结果；之后开始下半场
python3 scripts/codex_team/team.py phase second --minutes 40
# 队友上半场已结束本轮时，启动新一轮（读现有源码、历史和下半场提示词）
python3 scripts/codex_team/team.py resume

# 下半场结束：收卷最终源码
python3 scripts/codex_team/team.py phase finished --minutes 0
python3 scripts/codex_team/team.py stop
```

每个阶段收卷仅保存一次，避免重跑命令覆盖封存版本。计时和“停止修改”依赖参赛者遵守提示词，运行环境没有按秒自动冻结源码。这里提供两人开发与本地陪练测试，不自动生成其他队伍、全体单循环榜或正式裁判的逐球员系统调用隔离；三阶段提示词保留原赛制的评分和回退规则。

## 测试、续跑和新开一局

```sh
# Mac 原生联合对局：己方两人 vs baseline，2 个种子 × 换边
python3 scripts/codex_team/team.py test

# 显式在 agent 的 Linux 环境运行联合对局
python3 scripts/codex_team/team.py test-agent

# 验证网络策略、只读挂载、编译环境和基础两局
python3 scripts/codex_team/team.py doctor
python3 scripts/codex_team/team.py status

# 暂停沙箱；恢复时不清空源码和消息
python3 scripts/codex_team/team.py stop
python3 scripts/codex_team/team.py resume

# 新局：独立沙箱和目录，后续控制命令也加相同 --name
./start_codex_team.sh --name soccer-round2
python3 scripts/codex_team/team.py --name soccer-round2 phase first --minutes 40
```

同时运行多局时，Mac 模型服务可以共用端口，每个沙箱有自己的 loopback。浏览器可打开选手包中的 `viewer.html`，拖入回放文件。测试脚本：`PYTHONPYCACHEPREFIX=/tmp/soccer-pycache python3 -m unittest discover -s scripts/codex_team -v`。

## 隔离实现

沙箱只挂载自己的 `agent/`（读写）和 Mac 的 `submission/`（只读）；没有挂载仓库根目录、Mac 家目录、状态数据库或其他队伍资料。沙箱系统文件和编译工具仍正常可读。`/workspace` 指向自己的工作区，`/teammate/submission` 指向队友的只读提交目录；连 root 写入队友挂载都会失败。共享 skills 关闭，MCP 集合固定为空。

`--deny-network '**'` 禁止沙箱出站 TCP/UDP。沙箱 loopback HTTP 服务通过非交互 `sbx exec -i` 的 stdin/stdout JSONL 通道连接 Mac 中继；中继只接受固定的 Responses/compact/models 路由和队聊动作，固定上游、模型与认证，拒绝重定向，不转发任意 URL。流式响应逐块传输。无需开放 Mac 网络、端口转发或把 `host.docker.internal` 加入白名单。

若模型连接失败，查看 `state/worker.log` 和 `exit.json`，确认本机服务提供 Responses API、接受指定模型名和本机配置的网关密钥后运行 `resume`。sbx 创建时可能提示缺少 OpenAI 默认登录凭据；本流程使用独立的自定义 provider，不依赖默认登录。

配置和工具链依据：[Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)、[Docker 工具链 kit](https://docs.docker.com/learn/content/add-tools-and-runtimes-to-a-sandbox/)。

## 版本控制与干净仓库重建

| 保留并提交 | 忽略、不提交 |
|---|---|
| `scripts/codex_team/` 的脚本、prompt、文档、测试和工具链 kit | `.soccer-runs/` 全部：密钥、队聊、个人提交、日志、回放、状态备份、二进制 |
| `start_codex_team.sh`、`.gitignore`、文档入口 | Python 缓存、虚拟环境、`.DS_Store` |
| `workspace/` 原始工具源码和 `sample-replay.txt`，相关来源清单 | `unix_workspace/` 生成文件及本机 ELF/Mach-O 程序 |
| `build_unix_workspace.py`、`unix_workspace/arena.cpp` 和该目录的 `.gitignore` | 本地构建缓存和新产生的测试结果 |
| `sparring/bot_main.cpp`、`sparring/bots.hpp`，已有历史源码、成绩和归档回放 | 不要用通用 `replays/` 或 `*.json` 规则排除已有归档 |

`sample-replay.txt` 是原 `示例回放.txt` 的重命名，内容哈希不变。历史包里的原始文件名仍保留在清单的 `path` 字段，`published_source` 指向当前公开路径。

Debian 预编译 bot 继续被忽略。启动时会复用已有且架构匹配的 ELF；没有则使用 `sparring/` 源码和 `soccer.hpp`，在工具链 kit 提供的 Debian 环境中按 `-std=c++17 -O2 -pthread` 重建三支 bot，分别设置 `BOT_NAME=plan16 / plan32_b1.5 / pro_b0_n128`。源码通过脚本临时传入，构建后移除，不挂载整个仓库给 agent。

```sh
# 干净仓库：自动生成 Unix 工具包、构建 Mac 原生程序、创建受限沙箱及 Linux bot
python3 scripts/codex_team/team.py start --no-agent

# 显式重建现有沙箱中的 Debian bot
python3 scripts/codex_team/team.py rebuild-agent-bots
python3 scripts/codex_team/team.py test-agent

# 验证公开归档、重命名与清单一致性
python3 check_archive.py
```

新克隆的仓库没有本机密钥；正式启动 agent 前设置 `SOCCER_GATEWAY_API_KEY`，或创建被忽略的 `.soccer-runs/gateway.json`，格式为 `{"api_key":"你的密钥"}`。
