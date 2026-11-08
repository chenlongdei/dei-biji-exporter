# INSTALL.md — 给 AI Agent 的安装指令

你的用户想安装 biji-exporter。按下面的顺序执行，遇到问题优先看文末排错表。

## 1. 安装 skill

先弄清用户的 agent 类型，把 `skills/biji-exporter` 复制到对应 skills 目录：

| Agent | 安装目录 | Windows 下 |
| --- | --- | --- |
| Claude Code | `~/.claude/skills/` | `%USERPROFILE%\.claude\skills\` |
| WorkBuddy | `~/.workbuddy/skills/` | `%USERPROFILE%\.workbuddy\skills\` |
| 其他（OpenClaw/Codex 等） | 该 agent 的 skills 目录，结构不变 | 同理 |

```bash
mkdir -p ~/.claude/skills && cp -r <本文件夹路径>/skills/biji-exporter ~/.claude/skills/
```

只需 Python ≥3.9（macOS 自带；Windows 从 python.org 装，勾选 **Add python.exe to PATH**，命令用 `python` 而非 `python3`）。**表格合并零第三方依赖；导出功能需要 playwright（下一步装）。**

## 2. 创建配置

```bash
cd ~/.claude/skills/biji-exporter
cp config.example.json config.json
```

只读导出功能的用户到此即可开始；首次导出时按 SKILL.md 的「首次使用约定」问用户一次导出目录，写进 `biji_export_base`。

## 3. 导出功能：装 playwright 并登录（用户在场）

```bash
pip3 install --user playwright && python3 -m playwright install chromium
cd ~/.claude/skills/biji-exporter/scripts
python3 run.py login        # 会打开浏览器，用户扫码登录 biji.com
```

装过 playwright 的机器跳过安装直接 login。**引导原则**：只想用表格合并的用户完全不用做这一步。

## 4. 冒烟测试（免费）

```bash
python3 run.py export-biji "<用户订阅的某博主名>" --max-items 3
```

成功标准：导出目录出现 `{博主名}_{id}/` 下的 `.md` 逐字稿；输出 JSON 里 `exported: 3`。把产出路径告诉用户。

表格合并的冒烟：随手找两张 CSV 丢给 `python3 run.py merge-tables <文件...>`，看总表和 merge_report.md 生成。

## 排错表

| 症状 | 处理 |
| --- | --- |
| pip 装 playwright 报 externally-managed | 加 `--user`，或 `pip3 install --break-system-packages --user playwright` |
| login 打不开浏览器 | playwright 的 chromium 没装：`python3 -m playwright install chromium` |
| Get笔记 403 反复 | token 刷新失效 → 让用户重新 `run.py login`（人在场） |
| 网络错误带 proxy 字样 | 用户开了 VPN/代理，关掉重试；确实需要代理时设 `BIJI_USE_SYSTEM_PROXY=1` |
| export 说没找到博主 | 用户没订阅该博主，或名字有生僻字：`biji_export.py topics` + `follows` 列出来让用户认 |
| 合并时中文乱码 | 脚本已自动试 utf-8-sig/gbk/utf-8；仍失败让用户在 Excel 里另存为「CSV UTF-8」 |
| merge-tables 说 join 需要 key | 各表没有共同关键列：确认列名（视频id/链接/标题），用 `--key` 指定，或 `--mode stack` |
