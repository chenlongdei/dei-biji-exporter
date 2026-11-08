#!/usr/bin/env python3
"""biji-exporter 统一入口：登录、按名导出、合并表格。

  python3 run.py login                          # 浏览器登录得到大脑（首次/换号，一次）
  python3 run.py export-biji "谢胜子"            # 按博主名增量导出全部逐字稿
  python3 run.py export-biji "谢胜子" --max-items 50
  python3 run.py status "<订阅页URL>"            # 看导了多少、还差多少、从哪天续传
  python3 run.py merge-tables <文件/目录...>     # 合并数据表格（其余参数透传给 merge_tables.py）

更细的控制直接用子命令脚本：
  biji_export.py topics / follows <alias> / export "<url>" / status
  merge_tables.py --mode stack|join --key 列名 ...
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable  # 跟着当前解释器走，Windows 上也是它


def sh(cmd_args, desc, must=True):
    print(f"\n▶ {desc}\n  $ {' '.join(str(c) for c in cmd_args)}", flush=True)
    r = subprocess.run([str(c) for c in cmd_args], cwd=HERE)
    if r.returncode != 0 and must:
        print(f"\n✗ 「{desc}」失败（退出码 {r.returncode}），已停止。上面输出里有原因。")
        sys.exit(r.returncode)


def run_json(cmd_args):
    """跑子脚本拿 JSON stdout（错误时抛异常带输出）。"""
    r = subprocess.run([str(c) for c in cmd_args], cwd=HERE,
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        print("子命令输出异常：", r.stdout, r.stderr)
        sys.exit(2)


def cmd_login(args):
    sh([PY, "biji_export.py", "login"] + (["--force"] if args.force else []),
       "浏览器登录得到大脑（需要用户在场）")


def cmd_export_biji(args):
    # 按名字找博主：topics → follows 匹配
    topics = run_json([PY, "biji_export.py", "topics"]).get("items", [])
    target = None
    for t in topics:
        follows = run_json([PY, "biji_export.py", "follows", t["alias"]]).get("items", [])
        for f in follows:
            if args.name in f["name"]:
                target = f
                break
        if target:
            break
    if not target:
        print(f"在 {len(topics)} 个知识库里没找到名字含「{args.name}」的博主。"
              f"跑 `python3 biji_export.py topics` 看知识库，或直接用 URL："
              f"python3 biji_export.py export \"<url>\"")
        sys.exit(2)
    print(f"找到博主：{target['name']}（{target.get('post_count')} 篇）")
    limit = ["--max-items", str(args.max_items)] if args.max_items else []
    sh([PY, "biji_export.py", "export", target["url"]] + limit,
       f"增量导出逐字稿（{target['name']}）")


def cmd_status(args):
    sh([PY, "biji_export.py", "status", args.url] + (["--output", args.output] if args.output else []),
       "查看导出状态与续传建议")


def cmd_merge_tables(args):
    if not args.rest:
        print("用法：run.py merge-tables <文件或目录...> [merge_tables.py 的其他参数]")
        sys.exit(2)
    sh([PY, "merge_tables.py"] + args.rest, "合并数据表格")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("login", help="浏览器登录得到大脑（首次/换号）")
    lg.add_argument("--force", action="store_true", help="切换账号")
    lg.set_defaults(func=cmd_login)

    b = sub.add_parser("export-biji", help="按博主名增量导出逐字稿")
    b.add_argument("name", help="博主名（模糊匹配）")
    b.add_argument("--max-items", type=int, help="本次最多导出篇数")
    b.set_defaults(func=cmd_export_biji)

    st = sub.add_parser("status", help="查看某订阅页的导出状态与续传建议")
    st.add_argument("url", help="博主订阅页 URL")
    st.add_argument("--output", "-o", help="输出目录（不填用默认/历史目录）")
    st.set_defaults(func=cmd_status)

    mt = sub.add_parser("merge-tables", help="合并数据表格（参数透传 merge_tables.py）")
    mt.add_argument("rest", nargs=argparse.REMAINDER)
    mt.set_defaults(func=cmd_merge_tables)

    args = ap.parse_args()
    t0 = time.time()
    args.func(args)
    print(f"\n✔ 全部完成，用时 {time.time()-t0:.0f} 秒。")


if __name__ == "__main__":
    main()
