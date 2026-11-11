# INSTALL.md — 给 AI Agent 的安装指令

你的用户想安装 dei-biji-exporter。按下面的顺序执行，遇到问题优先看文末排错表。

## 1. 安装 skill

先弄清用户的 agent 类型，把 `skills/dei-biji-exporter` 复制到对应 skills 目录：

| Agent | 安装目录 | Windows 下 |
| --- | --- | --- |
| Claude Code | `~/.claude/skills/` | `%USERPROFILE%\.claude\skills\` |
| WorkBuddy | `~/.workbuddy/skills/` | `%USERPROFILE%\.workbuddy\skills\` |
| 其他（OpenClaw/Codex 等） | 该 agent 的 skills 目录，结构不变 | 同理 |

```bash
mkdir -p ~/.claude/skills && cp -r <本文件夹路径>/skills/dei-biji-exporter ~/.claude/skills/
```

只需 Python ≥3.9。**表格合并零第三方依赖；导出功能需要 playwright（第 3 步装）。**

- macOS：终端跑 `python3 --version`。如果弹窗要装「命令行开发者工具」，点安装，装完再继续（macOS 并不真正自带 Python）
- Windows：从 python.org 装，勾选 **Add python.exe to PATH**；之后所有命令用 `python` 而非 `python3`

### 从旧版 biji-exporter 升级（v1.2 起改名为 dei-biji-exporter）

如果 skills 目录里已经有旧的 `biji-exporter` 文件夹：
1. 把旧文件夹里的 `config.json`（如果有）复制到新的 `dei-biji-exporter/` 里
2. 删除旧的 `biji-exporter` 文件夹（否则 agent 会看到两个同功能的 skill）
3. **登录态不受影响**（仍在 `~/.biji_exporter/`），不用重新扫码
4. 如果旧版装过每日同步：先在旧目录里跑 `run.py schedule remove`，再在新目录里重新 `schedule install`

## 2. 创建配置

```bash
cd ~/.claude/skills/dei-biji-exporter
cp config.example.json config.json
```

只读导出功能的用户到此即可开始；首次导出时按 SKILL.md 的「首次使用约定」问用户一次导出目录，写进 `biji_export_base`。

## 3. 导出功能：自检 → 装依赖 → 登录（用户在场）

```bash
cd ~/.claude/skills/dei-biji-exporter/scripts
python3 run.py doctor       # 看哪项是 ❌，照 next_action 做；全绿直接 login
```

常见要做的（**一定用 `python3 -m pip`，别用 `pip3`**——两者可能指向不同的 Python，装了也找不到）：

```bash
python3 -m pip install --user playwright -i https://pypi.tuna.tsinghua.edu.cn/simple
# 只有电脑上既没有 Google Chrome 也没有 Edge 时才需要下面这行（约 150MB，国内可能较慢）：
python3 -m playwright install chromium
python3 run.py login        # 会打开浏览器，用户扫码登录 biji.com
```

**引导原则**：只想用表格合并的用户完全不用做这一步。

## 4. 冒烟测试（免费）

```bash
python3 run.py export-biji "<用户订阅的某博主名>" --max-items 3
```

成功标准：导出目录出现 `{博主名}_{id}/` 下的 `.md` 逐字稿；输出 JSON 里 `exported: 3`。把产出路径告诉用户。
若返回 `error_code: ambiguous`，把 `candidates` 给用户挑一个，用 `--url` 重跑。

表格合并的冒烟：随手找两张 CSV 丢给 `python3 run.py merge-tables <文件...>`，看总表和 merge_report.md 生成。

## 排错表

| 症状 | 处理 |
| --- | --- |
| 任何不确定 | `python3 run.py doctor`，照 `next_action` 做 |
| pip 装 playwright 报 externally-managed | `python3 -m pip install --user --break-system-packages playwright` |
| 装了 playwright 还说没装 | 用了 `pip3` 装到别的 Python 上了；改用 `python3 -m pip install --user playwright` |
| login 报找不到浏览器 | 装 Google Chrome；或 `python3 -m playwright install chromium` |
| 导出到一半 agent 超时 | 不丢数据，重跑同一命令续传；或改用 `--max-items 200` 分批 / 后台运行 |
| Get笔记 403 反复 | token 刷新失效 → 让用户重新 `run.py login`（人在场） |
| 网络错误带 proxy 字样 | 用户开了 VPN/代理，关掉重试；确实需要代理时设 `BIJI_USE_SYSTEM_PROXY=1` |
| export 说没找到博主 | 用户没订阅该博主，或名字有生僻字：`biji_export.py topics` + `follows` 列出来让用户认 |
| 合并时中文乱码 | 脚本已自动试 utf-8-sig/gbk/utf-8；仍失败让用户在 Excel 里另存为「CSV UTF-8」 |
| merge-tables 说 join 需要 key | 各表没有共同关键列：确认列名（视频id/链接/标题），用 `--key` 指定，或 `--mode stack` |
