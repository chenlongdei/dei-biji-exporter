# 安全与隐私

## 你的登录凭据存在哪里

| 内容 | 位置 | 会不会上传到 GitHub |
| --- | --- | --- |
| 得到大脑登录 token | `~/.biji_exporter/skill_credentials.json` | **不会**：在你的用户目录里，不在仓库里 |
| 浏览器登录态（cookie） | `~/.biji_exporter/browser_data_skill/` | **不会**：同上 |
| 导出目录等个人配置 | `skills/dei-biji-exporter/config.json` | **不会**：已写进 `.gitignore` |
| 导出的逐字稿 | 你在 config.json 里设置的目录（默认 `~/Downloads/Get笔记导出`） | **不会**：不在仓库里 |

- 工具**只**和得到大脑官方的服务器通信（`biji.com` / `knowledge-api.trytalks.com`），不会把你的数据发到任何第三方。
- 要彻底清除登录：删掉 `~/.biji_exporter` 文件夹即可。
- 换账号：`python3 run.py login --force`。

## 给贡献者：千万别提交这些

- `~/.biji_exporter/` 下的任何文件、`config.json`、导出的逐字稿、`sync.log`
- 提 Issue 时**不要**贴出 `token`、`csrf`、cookie。`doctor` 的输出不含凭据，可以放心贴。

仓库的 `.gitignore` 已经挡住了这些文件，但请在 `git add` 前再看一眼 `git status`。

## 报告安全问题

发现可能泄露用户凭据或数据的问题，请**不要**公开提 Issue，请通过 GitHub 的
[Private vulnerability reporting](../../security/advisories/new) 私下告诉作者。
