# 参与贡献

欢迎提 Issue 和 PR！这个工具的主要用户是**不写代码的学员**，所以改动请优先保证「装上就能用、出错时说人话」。

## 本地跑测试（不需要账号、不联网）

```bash
python3 -m unittest discover -s tests -v      # Windows 用 python
```

`tests/mock_server.py` 模拟了得到大脑的接口（包括 token 过期、限流空页、5xx 抖动），
`tests/test_offline.py` 会在「干净的学员电脑」条件下（独立数据目录、Windows 中文编码）跑完整流程。
每个 PR 都会自动在 **macOS / Windows / Linux × Python 3.9 / 3.12** 上跑一遍。

## 改代码时请遵守

1. **只用 Python 标准库**（登录用到的 playwright 除外）。学员电脑上装依赖是最容易出问题的环节。
2. **最低支持 Python 3.9**，别用 3.10+ 的语法（`match`、`X | None` 等）。
3. **stdout 只输出一个 JSON**，出错时带 `error_code` 和 `next_action`；进度写到 stderr。agent 靠这些判断下一步。
4. **不要写死本机路径**（v1.0 就因为写死了某个软件的浏览器路径，导致学员电脑上登录失败）。
5. 修 bug 请顺手加一个能复现它的测试。

## 提 Issue 前

先跑一下 `python3 run.py doctor`，把输出贴上（不含凭据，可以放心贴）。**不要贴 token / cookie。**
