#!/usr/bin/env python3
"""dei-biji-exporter 统一入口：自检、登录、按名导出、合并表格。

  python3 run.py doctor                         # 环境自检：缺什么、下一步跑什么（出问题先跑它）
  python3 run.py login                          # 浏览器登录得到大脑（首次/换号，一次）
  python3 run.py list                           # 列出账号里所有知识库和订阅的博主（含篇数）
  python3 run.py export-biji "谢胜子"            # 按博主名增量导出全部逐字稿
  python3 run.py export-biji "谢胜子" --start-date 2026-01-01 --end-date 2026-06-30   # 只导某段时间
  python3 run.py export-biji "谢胜子" --max-items 200   # 分批导（agent 命令有超时限制时用）
  python3 run.py export-biji --url "<订阅页URL>"  # 名字有歧义时用 URL 精确指定
  python3 run.py status "<订阅页URL>"            # 看导了多少、还差多少
  python3 run.py merge-tables <文件/目录...>     # 合并数据表格（其余参数透传给 merge_tables.py）

每日自动导出（监控新视频）：
  python3 run.py watch add "谢胜子"              # 加入关注清单（存在 config.json）
  python3 run.py watch list / watch remove "谢胜子"
  python3 run.py sync                           # 把关注清单里的博主都增量导一遍（--all = 所有订阅）
  python3 run.py schedule install --time 09:00  # 在本机注册每日定时任务（--dry-run 只看不装）
  python3 run.py schedule status / schedule remove

Windows 上把 python3 换成 python。
退出码：0 成功；2 出错（看 JSON 里的 error_code / next_action）；3 博主名找不到或有多个候选。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import auth_helper  # noqa: E402  (设置 utf-8 输出)
import biji_export as be  # noqa: E402

PY = sys.executable  # 跟着当前解释器走，Windows 上也是它
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
CONFIG = Path(os.environ.get("BIJI_CONFIG") or (HERE.parent / "config.json"))


def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _logged_in_creds():
    """返回 (token, csrf)；失败时 emit 错误 JSON 并返回 None。"""
    try:
        return be.refresh_credentials_if_needed(*be.load_credentials())
    except be.BijiError as e:
        emit(e.to_json())
    except Exception as e:
        emit(be.http_error_json(e) if hasattr(e, "code") else be.network_error_json(e))
    return None


def sh(cmd_args, desc):
    print(f"\n▶ {desc}", file=sys.stderr, flush=True)
    r = subprocess.run([str(c) for c in cmd_args], cwd=HERE, env=CHILD_ENV)
    return r.returncode


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2), flush=True)


def cmd_doctor(args):
    return sh([PY, "biji_export.py", "doctor"] + (["--skip-browser"] if args.skip_browser else []),
              "环境自检")


def cmd_login(args):
    return sh([PY, "biji_export.py", "login"] + (["--force"] if args.force else []),
              "浏览器登录得到大脑（需要用户在场扫码）")


def resolve_url(args):
    """返回 (url, name) 或 (None, 退出码)。"""
    if args.url:
        return args.url, be.get_follow_name_from_url(args.url)
    try:
        token, csrf = be.refresh_credentials_if_needed(*be.load_credentials())
        hit, cands = be.find_blogger(args.name, token, csrf)
    except be.BijiError as e:
        emit(e.to_json())
        return None, 2
    except Exception as e:
        emit(be.http_error_json(e) if hasattr(e, "code") else be.network_error_json(e))
        return None, 2
    if hit:
        print(f"找到博主：{hit['name']}（{hit.get('post_count') or '?'} 篇，知识库「{hit['topic_name']}」）",
              file=sys.stderr, flush=True)
        return hit["url"], hit["name"]
    if cands:
        emit({"ok": False, "error_code": "ambiguous",
              "error": f"名字含「{args.name}」的博主有 {len(cands)} 个，不确定是哪个",
              "candidates": [{"name": c["name"], "topic": c["topic_name"],
                              "post_count": c["post_count"], "url": c["url"]} for c in cands[:20]],
              "next_action": "把候选名单给用户选，再用完整名字重跑，或 run.py export-biji --url \"<候选url>\""})
    else:
        emit({"ok": False, "error_code": "not_found",
              "error": f"所有知识库里都没有名字含「{args.name}」的博主",
              "next_action": "确认用户在 Get笔记 订阅了该博主（导出只能导自己账号里订阅的）"})
    return None, 3


def cmd_export_biji(args):
    if not args.name and not args.url:
        print("用法：run.py export-biji \"博主名\"  或  run.py export-biji --url \"<订阅页URL>\"")
        return 2
    url, name = resolve_url(args)
    if not url:
        return name  # 这里是退出码
    extra = []
    if args.start_date:
        extra += ["--start-date", args.start_date]
    if args.end_date:
        extra += ["--end-date", args.end_date]
    if args.max_items:
        extra += ["--max-items", str(args.max_items)]
    if args.workers:
        extra += ["--workers", str(args.workers)]
    if args.output:
        extra += ["--output", args.output, "--output-is-base"]
    return sh([PY, "biji_export.py", "export", url] + extra, f"增量导出逐字稿（{name}）")


def cmd_list(args):
    """所有知识库 + 每个库里订阅的博主。stdout 是 JSON；--table 时额外在 stderr 打一张易读的表。"""
    creds = _logged_in_creds()
    if not creds:
        return 2
    token, csrf = creds
    watch_urls = {w.get("url") for w in load_config().get("watch", [])}
    topics = be.list_topics(token, csrf)
    out, n_follow = [], 0
    for t in topics:
        follows = be.list_follows(t["alias"], token, csrf)
        n_follow += len(follows)
        bl = []
        for f in follows:
            item = {"name": f["name"], "post_count": f["post_count"]}
            if f["url"] in watch_urls:
                item["watched"] = True
            if args.urls:
                item["url"] = f["url"]
            bl.append(item)
        out.append({"topic": t["name"], "bloggers": bl})
    for t in out:
        print(f"\n📚 知识库「{t['topic']}」（{len(t['bloggers'])} 位博主）", file=sys.stderr)
        for b in t["bloggers"]:
            print(f"   - {b['name']}  {b['post_count'] or '?'} 篇{'  ⏰已加入每日同步' if b.get('watched') else ''}",
                  file=sys.stderr)
    res = {"ok": True, "topic_count": len(out), "blogger_count": n_follow, "topics": out}
    if n_follow == 0:
        res["next_action"] = ("账号里还没有订阅任何博主：把 references/订阅博主教程.md 发给用户，"
                              "照着在得到大脑 App 里订阅一个，再回来导出")
    emit(res)
    return 0


def cmd_watch(args):
    cfg = load_config()
    watch = cfg.setdefault("watch", [])
    if args.action == "list":
        emit({"ok": True, "watch_all": bool(cfg.get("watch_all")), "watch": watch})
        return 0
    if args.action == "add":
        if not args.name and not args.url:
            print("用法：run.py watch add \"博主名\"  或  watch add --url \"<订阅页URL>\"")
            return 2
        if args.url:
            url, name = args.url, be.get_follow_name_from_url(args.url)
        else:
            url, name = resolve_url(args)
            if not url:
                return name
        if any(w["url"] == url for w in watch):
            emit({"ok": True, "message": f"「{name}」已经在关注清单里了", "watch": watch})
            return 0
        item = {"name": name, "url": url}
        if args.start_date:
            item["start_date"] = args.start_date
        watch.append(item)
        save_config(cfg)
        emit({"ok": True, "message": f"已把「{name}」加入每日同步清单", "watch": watch,
              "next_action": "还没装定时任务的话：run.py schedule install --time 09:00"})
        return 0
    if args.action == "remove":
        key = args.name or args.url
        keep = [w for w in watch if w["name"] != key and w["url"] != key]
        removed = len(watch) - len(keep)
        cfg["watch"] = keep
        save_config(cfg)
        emit({"ok": True, "removed": removed, "watch": keep})
        return 0


SYNC_LOCK_STALE = 3 * 3600


def cmd_sync(args):
    """把关注清单（或 --all 全部订阅）逐个增量导出；单个失败不影响其他。写 _同步日志.md。"""
    import scheduler
    lock = auth_helper.DATA_DIR / "sync.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and time.time() - lock.stat().st_mtime < SYNC_LOCK_STALE:
        emit({"ok": False, "error_code": "busy", "error": "已有一个同步在运行（或上次异常退出，3 小时后自动解除）"})
        return 2
    lock.write_text(str(os.getpid()))
    try:
        cfg = load_config()
        creds = _logged_in_creds()
        if not creds:
            scheduler.notify("dei-biji-exporter 每日同步失败", "登录已失效，请对 AI 说：重新登录得到大脑")
            return 2
        token, csrf = creds
        targets = []
        if args.all or cfg.get("watch_all"):
            for t in be.list_topics(token, csrf):
                for f in be.list_follows(t["alias"], token, csrf):
                    targets.append({"name": f["name"], "url": f["url"]})
        else:
            targets = list(cfg.get("watch", []))
        if not targets:
            emit({"ok": False, "error_code": "empty_watch",
                  "error": "关注清单是空的",
                  "next_action": "run.py watch add \"博主名\"，或 run.py sync --all 同步全部订阅"})
            return 2
        results = []
        for w in targets:
            if not args.quiet:
                print(f"\n▶ 同步「{w['name']}」", file=sys.stderr, flush=True)
            try:
                s = be.export(w["url"], token, csrf, start_date=w.get("start_date"),
                              on_progress=None if args.quiet else be._progress_printer(),
                              workers=args.workers or be.DEFAULT_WORKERS)
                results.append({"name": w["name"], "ok": s["ok"], "new": s["exported"],
                                "complete": s["complete"], "failed": s["failed"],
                                "output_dir": s["output_dir"], "error_code": s.get("error_code")})
                if s.get("error_code") == "token_expired":
                    break  # 登录彻底失效，后面的也不用试了
            except Exception as e:
                code = getattr(e, "code", None)
                results.append({"name": w["name"], "ok": False, "new": 0,
                                "error_code": ("token_expired" if code == 403 else
                                               type(e).__name__ if not isinstance(e, be.BijiError) else e.code),
                                "error": str(e)[:200]})
        new_total = sum(r.get("new", 0) for r in results)
        bad = [r for r in results if not r["ok"] or not r.get("complete", False)]
        _append_sync_log(results)
        if any(r.get("error_code") == "token_expired" for r in results):
            scheduler.notify("dei-biji-exporter 每日同步失败", "登录已失效，请对 AI 说：重新登录得到大脑")
        elif new_total:
            scheduler.notify("dei-biji-exporter", f"今日同步完成：新增 {new_total} 篇逐字稿")
        emit({"ok": not bad, "new_total": new_total, "results": results,
              "next_action": None if not bad else "有博主没同步完整，重跑 run.py sync（自动续传）"})
        return 0 if not bad else 2
    finally:
        try:
            lock.unlink()
        except Exception:
            pass


def _append_sync_log(results):
    p = Path(be.default_export_base_dir()) / "_同步日志.md"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with open(p, "a", encoding="utf-8") as f:
            if new:
                f.write("# 每日同步日志\n\n| 时间 | 博主 | 新增 | 状态 |\n| --- | --- | --- | --- |\n")
            ts = time.strftime("%Y-%m-%d %H:%M")
            for r in results:
                st = "✅" if r["ok"] and r.get("complete") else f"⚠️ {r.get('error_code') or '未完成'}"
                f.write(f"| {ts} | {r['name']} | {r.get('new', 0)} | {st} |\n")
    except Exception:
        pass


def cmd_schedule(args):
    import scheduler
    if args.action == "status":
        emit({"ok": True, **scheduler.status()})
        return 0
    if args.action == "remove":
        emit(scheduler.remove())
        return 0
    try:
        hh, mm = (int(x) for x in args.time.split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except Exception:
        emit({"ok": False, "error_code": "bad_input", "error": f"时间格式应为 HH:MM，收到 {args.time}"})
        return 2
    cfg = load_config()
    if args.all:
        cfg["watch_all"] = True
        save_config(cfg)
    if not cfg.get("watch") and not cfg.get("watch_all"):
        emit({"ok": False, "error_code": "empty_watch", "error": "关注清单是空的，定时任务装了也没东西可同步",
              "next_action": "先 run.py watch add \"博主名\"，或 schedule install --all 同步全部订阅"})
        return 2
    res = scheduler.install(hh, mm, dry_run=args.dry_run)
    warn = _macos_protected_dir_warning()
    if warn:
        res["warning"] = warn
    if res.get("ok") and not args.dry_run:
        res["note"] = ("电脑需要在这个时间开着（macOS 睡眠中错过的会在唤醒后补跑）；"
                       "得到大脑每天 8 点更新前一天的新视频，所以建议设在 8:30 之后")
    emit(res)
    return 0 if res.get("ok") else 2


def _macos_protected_dir_warning():
    """macOS 隐私保护（TCC）：后台定时任务里的 python 默认无权写 下载/桌面/文稿/外接硬盘，会静默失败。"""
    import platform
    if (os.environ.get("BIJI_FAKE_OS") or platform.system()) != "Darwin":
        return None
    base = Path(be.default_export_base_dir()).expanduser().resolve()
    home = Path.home().resolve()
    protected = [home / "Downloads", home / "Desktop", home / "Documents", Path("/Volumes")]
    hit = next((p for p in protected if base == p or p in base.parents), None)
    if not hit:
        return None
    return (f"导出目录 {base} 在 macOS 受保护的位置（{hit.name}），定时任务在后台可能没有权限写入。"
            f"二选一：① 把导出目录改到 {home}/Get笔记导出（改 config.json 的 biji_export_base）；"
            f"② 系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 加入 {sys.executable}")


def cmd_status(args):
    return sh([PY, "biji_export.py", "status", args.url] + (["--output", args.output] if args.output else []),
              "查看导出状态与续传建议")


def cmd_merge_tables(args):
    if not args.rest:
        print("用法：run.py merge-tables <文件或目录...> [merge_tables.py 的其他参数]")
        return 2
    return sh([PY, "merge_tables.py"] + args.rest, "合并数据表格")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="环境自检")
    d.add_argument("--skip-browser", action="store_true")
    d.set_defaults(func=cmd_doctor)

    lg = sub.add_parser("login", help="浏览器登录得到大脑（首次/换号）")
    lg.add_argument("--force", action="store_true", help="切换账号")
    lg.set_defaults(func=cmd_login)

    b = sub.add_parser("export-biji", help="按博主名增量导出逐字稿")
    b.add_argument("name", nargs="?", help="博主名（完全同名优先，其次包含匹配）")
    b.add_argument("--url", help="直接给订阅页 URL（名字有歧义时用）")
    b.add_argument("--start-date", help="只导这天及之后发布的（YYYY-MM-DD）")
    b.add_argument("--end-date", help="只导这天及之前发布的（YYYY-MM-DD）")
    b.add_argument("--max-items", type=int, help="本次最多导出篇数")
    b.add_argument("--workers", type=int, help="并发数（默认 3）")
    b.add_argument("--output", "-o", help="导出根目录（默认用 config.json 里的）")
    b.set_defaults(func=cmd_export_biji)

    ls = sub.add_parser("list", help="列出所有知识库和订阅的博主")
    ls.add_argument("--urls", action="store_true", help="附带每个博主的订阅页 URL（默认不带，省篇幅）")
    ls.set_defaults(func=cmd_list)

    w = sub.add_parser("watch", help="管理每日同步的关注清单")
    w.add_argument("action", choices=["add", "remove", "list"])
    w.add_argument("name", nargs="?", help="博主名")
    w.add_argument("--url", help="订阅页 URL（名字有歧义时用）")
    w.add_argument("--start-date", help="add 时可选：只同步这天之后的视频")
    w.set_defaults(func=cmd_watch)

    sy = sub.add_parser("sync", help="增量导出关注清单里的所有博主")
    sy.add_argument("--all", action="store_true", help="同步账号里全部订阅的博主")
    sy.add_argument("--workers", type=int)
    sy.add_argument("--quiet", action="store_true", help="不打逐篇进度（定时任务用）")
    sy.set_defaults(func=cmd_sync)

    sc = sub.add_parser("schedule", help="在本机注册/查看/删除每日自动同步")
    sc.add_argument("action", choices=["install", "status", "remove"])
    sc.add_argument("--time", default="09:00", help="每天几点跑（HH:MM，默认 09:00）")
    sc.add_argument("--all", action="store_true", help="同步全部订阅，而不只是关注清单")
    sc.add_argument("--dry-run", action="store_true", help="只显示要装什么，不真正安装")
    sc.set_defaults(func=cmd_schedule)

    st = sub.add_parser("status", help="查看某订阅页的导出状态与续传建议")
    st.add_argument("url", help="博主订阅页 URL")
    st.add_argument("--output", "-o", help="输出目录（不填用默认/历史目录）")
    st.set_defaults(func=cmd_status)

    mt = sub.add_parser("merge-tables", help="合并数据表格（参数透传 merge_tables.py）")
    mt.add_argument("rest", nargs=argparse.REMAINDER)
    mt.set_defaults(func=cmd_merge_tables)

    args = ap.parse_args()
    t0 = time.time()
    code = args.func(args) or 0
    mark = "✔ 完成" if code == 0 else f"✗ 未成功（退出码 {code}，看上面 JSON 的 next_action）"
    print(f"\n{mark}，用时 {time.time()-t0:.0f} 秒。", file=sys.stderr, flush=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
