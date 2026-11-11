---
name: dei-biji-exporter
description: 得到大脑/Get笔记 批量导出器 + 表格合并器。功能① 登录 Get笔记(biji.com) 订阅账号，列出知识库和订阅的博主，按博主名/时间范围批量导出逐字稿（Markdown，增量续传），可设置每天自动同步新视频；附在得到大脑里订阅抖音博主的教程；功能② 把多个数据表格（CSV/JSON，如学员反馈、多次导出的作品数据）合并成一张总表（同构表纵向堆叠去重、异构表按关键列横向匹配合并，自动处理 GBK 编码）。当用户说 导出得到大脑/Get笔记/导出逐字稿/批量导出博主/订阅博主/每天自动导出/监控博主更新/合并表格/合并CSV/汇总表格/表格合到一起 时使用。
---

# dei-biji-exporter — 得到大脑批量导出 + 表格合并

两件事，都很专一：

| | 导出 | 表格合并 |
| --- | --- | --- |
| 干什么 | 登录 Get笔记，把订阅博主**全部**逐字稿导成 Markdown | 多个 CSV/JSON 数据表 → 一张总表 |
| 入口 | `run.py list` → `run.py export-biji "博主名"`；每日自动：`watch` + `schedule` | `run.py merge-tables <文件/目录...>` |
| 底层 | `biji_export.py`（topics/follows/find/export/status/doctor）、`scheduler.py` | `merge_tables.py` |

## 导出：和用户对话的标准动线

所有命令在 `<skill>/scripts` 下执行；**Windows 把 `python3` 换成 `python`**。每一步都看 stdout 的 JSON，按 `next_action` 走。

**第 0 步 · 前置：用户在得到大脑里订阅了博主吗？**
导出只能导「用户自己账号里已订阅」的博主。用户说没订阅过、不知道怎么订，或者后面 `list` 显示 0 位博主 →
把 `references/订阅博主教程.md` 的内容给用户（手机 App 建知识库 → 抖音主页「分享名片」复制链接 → 粘贴订阅），等用户订完再继续。

**第 1 步 · 体检 + 登录**
```bash
python3 run.py doctor        # 逐项检查；全绿跳过下一行
python3 run.py login         # 用户在场扫码，一次即可，之后自动续期
```

**第 2 步 · 给用户看看账号里有什么**
```bash
python3 run.py list          # stderr 是易读清单（知识库 → 博主 → 篇数），stdout 是 JSON
```
把清单整理给用户看，然后**一次性问清三件事**（用户没说的用默认值，别反复追问）：

| 问什么 | 默认 | 对应参数 |
| --- | --- | --- |
| 导谁？（可以多个） | — 必须用户指定 | 博主名 |
| 什么时间范围？ | 全部历史 | `--start-date YYYY-MM-DD` / `--end-date YYYY-MM-DD` |
| 存到哪？ | config.json 里的目录；首次没配就问一次，回车用 `~/Downloads/Get笔记导出` | 写进 config.json 的 `biji_export_base`；单次可用 `--output` |

**第 3 步 · 导出**
```bash
python3 run.py export-biji "谢胜子"                                   # 全部
python3 run.py export-biji "谢胜子" --start-date 2026-10-01           # 只要 10 月以来的
```
- 实测速度：**725 篇约 2 分钟**（3 路并发）；增量幂等，重跑只补缺的
- 导完告诉用户：`output_dir`、新导了几篇；有 `empty_list` 就说明「这几条平台上没有逐字稿」，不是漏导

**第 4 步 · 顺带问一句：要不要每天自动导新视频？**（用户主动提“监控/每天/自动更新”时也走这里）
```bash
python3 run.py watch add "谢胜子"               # 加入关注清单（可多次 add 多个博主）
python3 run.py schedule install --time 09:00   # 在用户电脑上注册每日定时任务
python3 run.py schedule status                 # 确认装好了
```
- 得到大脑每天 **8:00** 更新前一天的新视频 → 定时设 **8:30 之后**
- 想同步账号里全部博主：`schedule install --all`
- 定时任务是装在**系统里**的（macOS launchd / Windows 任务计划程序 / Linux cron），不需要 agent 开着；电脑要开机（macOS 睡眠中错过的会在唤醒后补跑）
- 结果追加到导出目录下 `_同步日志.md`；macOS 会弹系统通知（新增几篇 / 登录失效）
- 返回里有 `warning`（macOS 导出目录在 下载/桌面/文稿/外接盘）→ **必须**转告用户并帮其二选一处理，否则后台任务可能写不进去
- 改时间：重新 `schedule install --time HH:MM`；停掉：`schedule remove`；移出清单：`watch remove "谢胜子"`
- 手动立刻同步一次：`python3 run.py sync`

### agent 必须遵守的执行规则

1. **看 JSON，不看感觉**：`complete: true` 才算导完；`complete: false` 就**重跑同一条命令**（自动续传），直到 true。
2. **命令有超时限制的 agent**（Claude Code 默认 2 分钟）：篇数多（>500）时后台运行（Claude Code 用 `run_in_background`），或分批 `--max-items 300` 循环跑。中途被杀不丢数据，重跑即续传。
3. **按 error_code 分支**：

| error_code | 含义 | 你该做的 |
| --- | --- | --- |
| `not_logged_in` / `token_expired` | 没登录 / 登录失效 | 请用户在场，跑 `run.py login` 扫码，再重跑原命令 |
| `ambiguous`（退出码 3） | 名字对应多个博主 | 把 `candidates` 列给用户选，再用 `--url "<候选url>"` 重跑 |
| `not_found`（退出码 3） | 账号里没订阅这个博主 | 走第 0 步，把订阅教程给用户 |
| `list_incomplete` | 平台限流，文章列表没取全 | 等 1~2 分钟重跑 |
| `network` | 网络/代理问题 | 让用户关 VPN/代理重试；确需代理设 `BIJI_USE_SYSTEM_PROXY=1` |
| `empty_watch` | 关注清单是空的 | `watch add` 加博主，或用 `--all` |
| `internal` | 工具 bug | 把整段 JSON 原样给用户，让其反馈给作者 |

## 表格合并：怎么选模式

```bash
python3 run.py merge-tables ~/Downloads/数据表/          # 目录递归收所有 csv/json
python3 run.py merge-tables a.csv b.json -o 总表.csv     # 指定文件和输出
python3 run.py merge-tables a.csv b.csv --mode join --key 视频id   # 强制横向合并
```

| 用户场景 | 模式 | 说明 |
| --- | --- | --- |
| 多张同格式表摞成一张（每月一张、每学员一张） | stack（默认） | 列取并集；按 视频id→链接→标题 自动去重，**重复时保留后面文件的新数据**（`--keep first` 反过来） |
| A 表有点赞数、B 表有评论数，按视频合成宽表 | join | 第一张为主表，按 key 匹配；列冲突自动加 _2 |
| 不知道哪种 | auto（默认） | 列基本一致→stack；列不同但有共同 key→join |

- 支持 CSV / TSV / JSON / **xlsx**（零依赖读第一个工作表；老 .xls 让用户另存为 xlsx）
- 编码自动识别 utf-8 / utf-16 / **gb18030(含 GBK)**；分隔符自动识别；单元格内换行保留
- 链接列匹配前自动去掉 `?参数` 和末尾 `/`；目录里上次合并出的 `merged_*.csv` 会自动跳过
- 输出：utf-8-sig CSV（Excel 双击不乱码）+ `merge_report.md`（每表几行、去重多少、join 匹配率）
- 坏文件自动跳过并记进报告，不会一颗老鼠屎坏一锅粥

## 排错速查

| 现象 | 处理 |
| --- | --- |
| 不知道哪里不对 | `python3 run.py doctor`，照 `next_action` 做 |
| Get笔记 403 反复出现 | `python3 run.py login`（用户在场，一次） |
| 网络错误带 proxy 字样 | 用户开了 VPN/代理，关掉或在环境变量 `BIJI_USE_SYSTEM_PROXY=1` 下重试 |
| playwright 未安装 | `python3 -m pip install --user playwright`（国内加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`） |
| 找不到浏览器 | 装 Google Chrome 最省事；或 `python3 -m playwright install chromium` |
| 合并乱码/编码报错 | 脚本已自动试三种编码仍失败时，让用户用 Excel 另存为「CSV UTF-8」再丢进来 |
| join 匹配率低 | 看 merge_report.md 未匹配行；确认各表 key 列内容一致（链接带不带参数、id 前后空格） |
| Windows | 命令用 `python` 而非 `python3`；路径同理 |

## 首次使用约定（agent 行为）

1. **首次导出前问用户一次**：「导出的素材想放哪个文件夹？回车默认用下载文件夹（~/Downloads/Get笔记导出）」。回答后写进 `config.json` 的 `biji_export_base`（相对路径转绝对），不问第二次。如果用户已经说过要每天自动同步，macOS 上建议默认用 `~/Get笔记导出`（不在系统保护目录里）。
2. 合并表格**不需要任何配置**，拿到的路径直接丢给 merge-tables。
3. 用户环境 macOS 或 Windows 均可：脚本纯 Python 标准库；登录线要 playwright。
4. 装到哪个 agent 都行（Claude Code `~/.claude/skills/`，WorkBuddy `~/.workbuddy/skills/`）。
