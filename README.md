# dei-biji-exporter — 得到大脑批量导出 + 表格合并

[![offline-tests](https://github.com/chenlongdei12-dev/dei-biji-exporter/actions/workflows/test.yml/badge.svg)](https://github.com/chenlongdei12-dev/dei-biji-exporter/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![macOS | Windows](https://img.shields.io/badge/platform-macOS%20%7C%20Windows-lightgrey)

两件事：**① 登录得到大脑（Get笔记），把订阅博主的历史内容整包导出成逐字稿；② 把一堆数据表格合并成一张总表。**

## 🚀 一句话安装

打开你的 AI（Claude Code / WorkBuddy / 其他支持 skills 的 agent），把下面这句话**原样**发给它：

```text
帮我安装这个 skill，按仓库里的 INSTALL.md 操作：https://github.com/chenlongdei12-dev/dei-biji-exporter
```

AI 会自己下载、安装、检查环境，然后打开浏览器让你**扫码登录一次**得到大脑。之后直接说大白话就行，比如「把谢胜子的视频逐字稿全部导出来」。

> 还没在得到大脑里订阅过博主？先看 👉 [前置教程：订阅一个抖音博主](skills/dei-biji-exporter/references/subscribe-guide.md)（手机 3 分钟，免费账号最多可订 30 个）

## 它能干嘛

| 你想要 | 它做 | 用时 |
| --- | --- | --- |
| 「把这个博主**全部**视频的文字导出来」 | 登录得到大脑 → 按博主名一键导出，Markdown 一个文件夹 | 700 篇约 2 分钟 |
| 「我订阅了哪些博主？」 | 列出所有知识库、博主和篇数 | 几秒 |
| 「只要他最近 3 个月的」 | 按时间范围导出 | 看篇数 |
| 「以后他发新视频自动帮我导」 | 在你电脑上设一个每天定时任务，自动补新视频（Mac / Windows 都行） | 设一次 |
| 「上次导到一半断了」 | 自动续传，只补缺的，已导的不动 | 看缺多少 |
| 「这几张数据表给我合成一张」 | 目录一丢，自动判断摞起来还是按视频匹配合并，Excel 打开不乱码 | 秒级 |

## 安装细节（一句话安装背后发生了什么）

**你只需要：一台 Mac 或 Windows 电脑 + 装了 Claude Code 或 WorkBuddy（或任何会用 skills 的 AI agent）+ 一个得到大脑（Get笔记）账号，并在里面订阅了想导出的抖音博主。**

1. **下载安装**：AI 按 [INSTALL.md](INSTALL.md) 把代码下载下来，复制到它的 skills 目录，并检查 Python、浏览器等环境
   （AI 访问不了 GitHub 时的备用办法：本页右上角绿色 **Code → Download ZIP**，解压后把整个文件夹拖给 AI，说「帮我安装这个工具包，按 INSTALL.md 操作」）
2. **登录一次**得到大脑：AI 会打开浏览器让你扫码，之后一直免登录（token 自动刷新）
3. 完事。直接说大白话：「把谢胜子的内容全导出来」「把下载文件夹里那几张表合成一张」

> 电脑上有 Google Chrome 或 Edge（Windows 自带）就够了，不用额外下载浏览器。

**费用：¥0。** 走的是你自己账号里已订阅的内容，不需要任何 API key。

## 表格合并怎么用

| 情况 | 它会怎么做 |
| --- | --- |
| 多张格式一样的表（每月一张/每人一张） | 上下摞成一张，按视频/链接自动去重 |
| A 表有点赞数、B 表有评论数 | 按视频链接匹配合成一张宽表 |
| Excel/WPS 导出的中文 CSV（GBK 编码）、xlsx | 自动识别，不会乱码 |

输出用 Excel 直接打开就是正常中文表，旁边还有一份 `merge_report.md` 说明每张表进了多少行、去重了多少。

## 常见问题

- **导出的前提是什么？** 你在得到大脑（Get笔记）里**订阅了**该博主——工具只能导你账号里有的内容。免费账号最多可订阅 30 个博主。
- **我的登录信息安全吗？** 只保存在你自己电脑的 `~/.biji_exporter/` 里，只和得到大脑官方服务器通信，不会上传到任何别的地方。详见 [SECURITY.md](SECURITY.md)。
- **会不会封号？** 走官方网页接口，最多 3 路并发、每次请求带随机间隔，不做高频爬取。请仅用于个人学习研究，遵守平台条款。
- **出问题了怎么办？** 对 AI 说「跑一下 dei-biji-exporter 的 doctor」，它会逐项检查并告诉你下一步。
- **Windows 能用吗？** 能。脚本纯 Python 标准库；登录功能要装一次 playwright（AI 会引导）。
- **我是 WorkBuddy 用户行吗？** 行，装到 `~/.workbuddy/skills/` 即可，用法完全一样。

## 怎么升级

同样一句话：

```text
帮我升级 dei-biji-exporter，按仓库里的 INSTALL.md 操作：https://github.com/chenlongdei12-dev/dei-biji-exporter
```

会保留你的 config.json、登录态和所有导出数据，不用重新扫码。

## 开源协议

本项目以 **[MIT 协议](LICENSE)** 开源。简单说：

- ✅ 可以免费使用、复制、修改、分发，包括用于商业用途
- ✅ 可以基于它二次开发、集成进你自己的工具或课程
- 📌 唯一要求：在副本或衍生作品里**保留原作者署名和 LICENSE 文件**
- ⚠️ 软件按「现状」提供，作者不对使用结果承担责任（见下方免责声明）

> v1.0 曾以 CC BY-NC-SA 4.0 发布；从 v1.2 起改为 MIT。

## 免责声明

- 本项目是个人开发的**非官方**工具，与「得到」「得到大脑」「Get笔记」及其运营方没有任何关联，也未获得其授权或背书。
- 工具只是帮你批量保存**你自己账号里已有权访问**的内容，不绕过任何付费或权限限制。请遵守得到大脑的用户协议，合理控制使用频率。
- 导出的逐字稿的著作权属于**原视频创作者**。请仅用于个人学习研究，**不要未经授权公开转载、售卖或冒用**。
- 平台接口随时可能变化导致工具失效；因使用本工具产生的任何后果由使用者自行承担。

## 参与贡献

欢迎提 Issue 和 PR，请先读 [CONTRIBUTING.md](CONTRIBUTING.md)。遇到问题提 Issue 时，附上 `doctor` 的输出（**不要贴 token**）。
更新记录见 [CHANGELOG.md](CHANGELOG.md)。
