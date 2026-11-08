---
name: biji-exporter
description: 得到大脑/Get笔记 批量导出器 + 表格合并器。功能① 登录 Get笔记(biji.com) 订阅账号，按博主名或订阅页 URL 批量导出全部逐字稿（Markdown，增量续传）；功能② 把多个数据表格（CSV/JSON，如学员反馈、多次导出的作品数据）合并成一张总表（同构表纵向堆叠去重、异构表按关键列横向匹配合并，自动处理 GBK 编码）。当用户说 导出得到大脑/Get笔记/导出逐字稿/批量导出博主/合并表格/合并CSV/汇总表格/表格合到一起 时使用。
---

# biji-exporter — 得到大脑批量导出 + 表格合并

两件事，都很专一：

| | 导出 | 表格合并 |
| --- | --- | --- |
| 干什么 | 登录 Get笔记，把订阅博主**全部**逐字稿导成 Markdown | 多个 CSV/JSON 数据表 → 一张总表 |
| 入口 | `run.py login` → `run.py export-biji "博主名"` | `run.py merge-tables <文件/目录...>` |
| 底层 | `biji_export.py`（topics/follows/export/status） | `merge_tables.py` |

## 导出：标准动线

```bash
cd <skill>/scripts
python3 run.py login                # 首次/换号时一次，用户在场扫码
python3 run.py export-biji "谢胜子"  # 名字模糊匹配 → 增量导出全部逐字稿
python3 run.py status "<url>"       # 中断后看进度和续传建议
```

- 导出是**增量幂等**的：文件名带 post_id，重跑自动跳过已导出的，只补新的
- 前置依赖：`pip3 install --user playwright && python3 -m playwright install chromium`（仅登录线需要；已装过的直接用）
- token 30 分钟过期，脚本会用登录态**无头自动刷新**；反复 403 才需要用户重新 `login`

## 表格合并：怎么选模式

```bash
python3 run.py merge-tables ~/Downloads/数据表/          # 目录递归收所有 csv/json
python3 run.py merge-tables a.csv b.json -o 总表.csv     # 指定文件和输出
python3 run.py merge-tables a.csv b.csv --mode join --key 视频id   # 强制横向合并
```

| 用户场景 | 模式 | 说明 |
| --- | --- | --- |
| 多张同格式表摞成一张（每月一张、每学员一张） | stack（默认） | 列取并集；按 视频id→链接→标题 自动去重 |
| A 表有点赞数、B 表有评论数，按视频合成宽表 | join | 第一张为主表，按 key 匹配；列冲突自动加 _2 |
| 不知道哪种 | auto（默认） | 列基本一致→stack；列不同但有共同 key→join |

- CSV 自动试 utf-8-sig / **gbk** / utf-8（Excel/WPS 导出的中文表直接丢进来就行）
- 输出：utf-8-sig CSV（Excel 双击不乱码）+ `merge_report.md`（每表几行、去重多少、join 匹配率）
- 坏文件自动跳过并记进报告，不会一颗老鼠屎坏一锅粥

## 排错速查

| 现象 | 处理 |
| --- | --- |
| Get笔记 403 反复出现 | `python3 run.py login`（用户在场，一次） |
| not_logged_in | 同上，先 login |
| 网络错误带 proxy 字样 | 用户开了 VPN/代理，关掉或在环境变量 `BIJI_USE_SYSTEM_PROXY=1` 下重试 |
| playwright 未安装 | `pip3 install --user playwright && python3 -m playwright install chromium` |
| 合并乱码/编码报错 | 脚本已自动试三种编码仍失败时，让用户用 Excel 另存为「CSV UTF-8」再丢进来 |
| join 匹配率低 | 看 merge_report.md 未匹配行；确认各表 key 列内容一致（链接带不带参数、id 前后空格） |
| Windows | 命令用 `python` 而非 `python3`；路径同理 |

## 首次使用约定（agent 行为）

1. **首次导出前问用户一次**：「导出的素材想放哪个文件夹？回车默认用下载文件夹（~/Downloads/Get笔记导出）」。回答后写进 `config.json` 的 `biji_export_base`（相对路径转绝对），不问第二次。
2. 合并表格**不需要任何配置**，拿到的路径直接丢给 merge-tables。
3. 用户环境 macOS 或 Windows 均可：脚本纯 Python 标准库；登录线要 playwright。
4. 装到哪个 agent 都行（Claude Code `~/.claude/skills/`，WorkBuddy `~/.workbuddy/skills/`）。
