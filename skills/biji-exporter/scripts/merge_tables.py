#!/usr/bin/env python3
"""表格合并器：多个 CSV/JSON 数据表 → 一张总表（utf-8-sig CSV + merge_report.md）

用法：
  python3 merge_tables.py <文件或目录> [更多输入...] [-o 输出.csv] [--mode auto|stack|join] [--key 列名] [--no-dedupe]

- 输入是目录时递归收集里面所有 *.csv / *.json（跳过隐藏文件与子目录里的报告）
- CSV 自动尝试 utf-8-sig / gbk / utf-8 编码——Excel/WPS 导出的中文 CSV 多为 GBK
- 合并模式：
    stack 纵向堆叠：所有表上下拼接，列取并集；默认按 key 去重（保留先出现的）
    join  横向匹配：第一张表为主表，后面的表按 key 列匹配进来，列名冲突自动加 _2/_3 后缀
    auto  自动判断（默认）：各表列基本一致 → stack；列不同但共享关键列 → join
- key 自动猜测顺序：视频id/video_id/aweme_id → 视频链接/链接/link/share_url/url → 标题/title → id
  都没有时 stack 改用整行去重；join 找不到 key 时会报错，请 --key 指定
- 输出：CSV（utf-8-sig，Excel 双击打开不乱码）+ 同目录 merge_report.md + stdout JSON 摘要
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

KEY_CANDIDATES = [
    ("视频id", "video_id", "aweme_id"),
    ("视频链接", "链接", "link", "share_url", "url"),
    ("标题", "title"),
    ("id",),
]


# ---------- 读取 ----------

def collect_files(paths):
    files = []
    for p in paths:
        p = Path(p).expanduser()
        if p.is_dir():
            found = [f for f in p.rglob("*")
                     if f.suffix.lower() in (".csv", ".json")
                     and not f.name.startswith(".")
                     and f.name != "merge_report.md"]
            files.extend(sorted(found))
        elif p.is_file():
            files.append(p)
        else:
            raise SystemExit(f"[错误] 路径不存在：{p}")
    seen, out = set(), []
    for f in files:
        k = str(f.resolve())
        if k not in seen:
            seen.add(k)
            out.append(f)
    if not out:
        raise SystemExit("[错误] 没有找到任何 .csv / .json 文件")
    return out


def _clean_cell(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()


def read_csv_rows(path):
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "gbk", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("无法识别编码（试过 utf-8-sig/gbk/utf-8）")
    rows = []
    for r in csv.DictReader(text.splitlines()):
        row = {}
        for k, v in r.items():
            if k is None:  # restkey：行尾多余列
                if isinstance(v, list):
                    v = ";".join(str(x) for x in v)
                row["_extra"] = _clean_cell(v)
                continue
            row[str(k).strip()] = _clean_cell(v)
        if any(v for v in row.values()):
            rows.append(row)
    return rows


def read_json_rows(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = None
        for key in ("items", "rows", "data", "list"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        if items is None:
            items = next((v for v in data.values() if isinstance(v, list)), [])
    else:
        raise ValueError("JSON 顶层不是数组也不是带数组的对象")
    rows = []
    for it in items:
        if isinstance(it, dict):
            rows.append({str(k).strip(): _clean_cell(v) for k, v in it.items()})
        else:
            rows.append({"value": _clean_cell(it)})
    return rows


def read_table(path):
    if path.suffix.lower() == ".json":
        return read_json_rows(path)
    return read_csv_rows(path)


# ---------- 列与 key ----------

def all_keys(rows):
    ks = []
    for row in rows:
        for k in row:
            if k not in ks:
                ks.append(k)
    return ks


def guess_key(tables):
    """找一个所有表都有、且多数行非空的列名；找不到返回 None。"""
    common = set.intersection(*[set(all_keys(t)) for t in tables])
    total = sum(len(t) for t in tables)
    for group in KEY_CANDIDATES:
        for cand in group:
            if cand not in common:
                continue
            nonempty = sum(1 for t in tables for r in t if r.get(cand))
            if total and nonempty / total >= 0.5:
                return cand
    return None


def decide_mode(tables):
    """auto：列基本一致 → stack；列差异大但有共同 key → join。
    两种情况都尽量带出 guessed key（stack 用它去重更准）。"""
    col_sets = [set(all_keys(t)) for t in tables]
    key = guess_key(tables)
    main = col_sets[0]
    similar = all(len(cs & main) >= 0.6 * max(len(cs), len(main)) for cs in col_sets)
    if similar:
        return "stack", key
    return ("join", key) if key else ("stack", None)


# ---------- 合并 ----------

def do_stack(tables, key, dedupe=True):
    cols = []
    for t in tables:
        for k in all_keys(t):
            if k not in cols:
                cols.append(k)
    if key is None and dedupe:  # 整行去重只比所有表共有的列，避免空串差异
        common_cols = [c for c in cols if all(c in all_keys(t) for t in tables)]
    else:
        common_cols = None
    rows, seen, dropped = [], set(), 0
    for t in tables:
        for r in t:
            if dedupe:
                kv = r.get(key, "") if key else tuple(r.get(c, "") for c in common_cols)
                if not kv:  # key 为空的行不去重，如实保留
                    rows.append(r)
                    continue
                if kv in seen:
                    dropped += 1
                    continue
                seen.add(kv)
            rows.append(r)
    return cols, rows, dropped


def do_join(tables, key):
    main_rows = [dict(r) for r in tables[0]]
    main_cols = all_keys(tables[0])
    idx = {}
    for i, r in enumerate(main_rows):
        kv = r.get(key, "")
        if kv and kv not in idx:
            idx[kv] = i
    extra_cols, match_stats = [], []
    for t in tables[1:]:
        t_keys = all_keys(t)
        rename = {}
        for k in t_keys:
            if k == key:
                continue
            if k in main_cols or k in extra_cols:
                base, n = k, 2
                while f"{base}_{n}" in main_cols or f"{base}_{n}" in extra_cols:
                    n += 1
                rename[k] = f"{base}_{n}"
        t_idx = {}
        for r in t:
            kv = r.get(key, "")
            if kv and kv not in t_idx:
                t_idx[kv] = r
        matched = 0
        for kv, i in idx.items():
            src = t_idx.get(kv)
            if not src:
                continue
            matched += 1
            for k, v in src.items():
                if k == key:
                    continue
                dst = rename.get(k, k)
                main_rows[i][dst] = v
                if dst not in main_cols and dst not in extra_cols:
                    extra_cols.append(dst)
        match_stats.append({"rows": len(t), "distinct_keys": len(t_idx),
                            "matched": matched, "unmatched": len(t_idx) - matched})
    return main_cols + extra_cols, main_rows, match_stats


# ---------- 输出 ----------

def write_csv(path, cols, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_report(path, files, per_file, skipped, mode, key, cols, rows,
                 dropped, match_stats, out_csv):
    lines = ["# 表格合并报告", "",
             f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 模式：{mode}（{ '按整行去重' if mode == 'stack' and not key else '关键列：' + key if key else '未去重' }）",
             f"- 输入：{len(files)} 个文件，成功 {len(per_file)} 个" + (f"，跳过 {len(skipped)} 个" if skipped else ""),
             f"- 输出：{out_csv.name}（{len(rows)} 行 × {len(cols)} 列）", "",
             "## 输入文件", "",
             "| 文件 | 行数 |", "| --- | --- |"]
    for pf in per_file:
        lines.append(f"| {pf['file']} | {pf['rows']} |")
    for s in skipped:
        lines.append(f"| {s['file']} | 跳过（{s['reason']}） |")
    if mode == "stack":
        lines += ["", f"- 去重删除 {dropped} 行（key 重复，保留先出现的）"]
    else:
        lines += ["", "## 各表匹配情况（join）", "",
                  "| 表 | 总行数 | 不同key | 匹配上主表 | 没匹配上 |", "| --- | --- | --- | --- | --- |"]
        for i, ms in enumerate(match_stats, 2):
            lines.append(f"| 表{i} | {ms['rows']} | {ms['distinct_keys']} | {ms['matched']} | {ms['unmatched']} |")
    lines += ["", f"- 输出总行数：{len(rows)}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="表格合并器：多个 CSV/JSON → 一张总表")
    ap.add_argument("inputs", nargs="+", help="CSV/JSON 文件或目录（目录递归收集）")
    ap.add_argument("-o", "--output", help="输出 CSV 路径（默认在第一个输入旁 merged_时间戳.csv）")
    ap.add_argument("--mode", choices=["auto", "stack", "join"], default="auto",
                    help="合并模式：stack 纵向堆叠去重 / join 按关键列横向合并 / auto 自动判断")
    ap.add_argument("--key", help="去重/匹配用的列名（默认自动猜：视频id→链接→标题→id）")
    ap.add_argument("--no-dedupe", action="store_true", help="stack 模式不去重，全部保留")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()

    files = collect_files(args.inputs)
    tables, per_file, skipped = [], [], []
    for f in files:
        try:
            t = read_table(f)
            if not t:
                raise ValueError("表格为空")
        except Exception as e:
            skipped.append({"file": str(f), "reason": str(e)[:80]})
            continue
        tables.append(t)
        per_file.append({"file": str(f), "rows": len(t)})
    if not tables:
        raise SystemExit("[错误] 没有成功读入的表格")

    mode, key = args.mode, args.key
    if mode == "auto":
        mode, guessed = decide_mode(tables)
        key = key or guessed
    if mode == "join" and not key:
        key = guess_key(tables)
    if mode == "join" and not key:
        raise SystemExit("[错误] join 模式需要共同的关键列，请用 --key 指定（或改用 --mode stack）")

    if mode == "stack":
        cols, rows, dropped = do_stack(tables, key, dedupe=not args.no_dedupe)
        match_stats = []
    else:
        cols, rows, match_stats = do_join(tables, key or "")
        dropped = 0

    out_csv = (Path(args.output).expanduser() if args.output
               else files[0].parent / f"merged_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    write_csv(out_csv, cols, rows)
    report = out_csv.parent / "merge_report.md"
    write_report(report, files, per_file, skipped, mode, key, cols, rows,
                 dropped, match_stats, out_csv)

    print(json.dumps({"ok": True, "mode": mode, "key": key,
                      "files": len(files), "read_ok": len(tables),
                      "skipped": skipped,
                      "rows_in": sum(pf["rows"] for pf in per_file),
                      "rows_out": len(rows), "cols_out": len(cols),
                      "deduped": dropped, "join_stats": match_stats,
                      "output_csv": str(out_csv), "report": str(report)},
                     ensure_ascii=False, indent=2 if args.pretty else None))


if __name__ == "__main__":
    main()
