#!/usr/bin/env python3
"""表格合并器：多个 CSV/JSON 数据表 → 一张总表（utf-8-sig CSV + merge_report.md）

用法：
  python3 merge_tables.py <文件或目录> [更多输入...] [-o 输出.csv] [--mode auto|stack|join] [--key 列名] [--no-dedupe]

- 输入是目录时递归收集里面所有 *.csv / *.tsv / *.json / *.xlsx
  （跳过隐藏文件、Excel 临时文件 ~$xxx、以及之前合并出来的 merged_*.csv）
- CSV 自动识别编码 utf-8 / utf-16 / gb18030(含 GBK)，自动识别分隔符（逗号/Tab/分号），单元格内换行保留
- xlsx 零依赖读取第一个工作表（日期列会是 Excel 序号数字）
- 合并模式：
    stack 纵向堆叠：所有表上下拼接，列取并集；默认按 key 去重（保留先出现的）
    join  横向匹配：第一张表为主表，后面的表按 key 列匹配进来，列名冲突自动加 _2/_3 后缀
    auto  自动判断（默认）：各表列基本一致 → stack；列不同但共享关键列 → join
- key 自动猜测顺序：视频id/video_id/aweme_id → 视频链接/链接/link/share_url/url → 标题/title → id
  都没有时 stack 改用整行去重；join 找不到 key 时会报错，请 --key 指定
- key 比较前会规范化：去首尾空格、链接去掉 ?参数 和末尾 /、"123.0" 视为 "123"
- stack 去重默认保留**后出现**的数据（按文件名排序，后面的月份覆盖前面的；空单元格用旧值补），--keep first 可改
- 输出：CSV（utf-8-sig，Excel 双击打开不乱码）+ 同目录 merge_report.md + stdout JSON 摘要
"""
import argparse
import csv
import io
import json
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

for _s in (sys.stdout, sys.stderr):  # Windows 中文系统下被 agent 捕获输出时防乱码/崩溃
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TABLE_SUFFIXES = (".csv", ".tsv", ".json", ".xlsx")

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
                     if f.is_file()
                     and f.suffix.lower() in TABLE_SUFFIXES
                     and not f.name.startswith((".", "~$", "merged_"))
                     and f.name != "links.json"]
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
        raise SystemExit("[错误] 没有找到任何 .csv / .tsv / .json / .xlsx 文件")
    return out


def _clean_cell(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()


def decode_bytes(raw):
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):  # Excel「Unicode 文本」导出是 UTF-16
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "gb18030"):  # gb18030 是 GBK 的超集，生僻字也能解
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别编码（试过 utf-8/utf-16/gb18030）")


def read_csv_rows(path):
    text = decode_bytes(path.read_bytes())
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
        delim = dialect.delimiter
    except csv.Error:
        delim = "\t" if path.suffix.lower() == ".tsv" else ","
    return _dict_rows(csv.reader(io.StringIO(text, newline=""), delimiter=delim))


def _dict_rows(row_iter):
    """首个非空行当表头，其余转 dict；多出的列放 _extra。"""
    header, rows = None, []
    for raw in row_iter:
        if header is None:
            if any(str(c).strip() for c in raw):
                header = [str(c).strip() or f"列{i+1}" for i, c in enumerate(raw)]
            continue
        r = dict(zip(header, raw))
        if len(raw) > len(header):
            r[None] = raw[len(header):]
        rows.append(r)
    return _clean_rows(rows)


def _clean_rows(dict_rows):
    rows = []
    for r in dict_rows:
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
    data = json.loads(path.read_text(encoding="utf-8-sig"))
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


_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _col_index(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx_num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return v
    if x.is_integer() and abs(x) < 1e15:
        return str(int(x))
    return repr(x)


def read_xlsx_rows(path):
    """零依赖读 xlsx 第一个工作表。"""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_NS + "t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        first = next(wb.iter(_NS + "sheet"))
        rid = first.get(_RNS + "id")
        target = "xl/worksheets/sheet1.xml"
        if "xl/_rels/workbook.xml.rels" in names:
            for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
                if rel.get("Id") == rid:
                    t = rel.get("Target", "")
                    target = t.lstrip("/") if t.startswith("/") else "xl/" + t
        sheet = ET.fromstring(z.read(target))
    grid = []
    for row in sheet.iter(_NS + "row"):
        cells, pos = {}, 0
        for c in row.findall(_NS + "c"):
            ref = c.get("r")
            pos = _col_index(ref) if ref else pos
            t, v = c.get("t"), c.find(_NS + "v")
            if t == "s":
                val = shared[int(v.text)] if v is not None and v.text else ""
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(_NS + "t"))
            elif t in ("str", "b", "e"):
                val = v.text if v is not None else ""
            else:
                val = _xlsx_num(v.text) if v is not None else ""
            cells[pos] = val
            pos += 1
        grid.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return _dict_rows(grid)


def read_table(path):
    suf = path.suffix.lower()
    if suf == ".json":
        return read_json_rows(path)
    if suf == ".xlsx":
        return read_xlsx_rows(path)
    if suf == ".xls":
        raise ValueError("老式 .xls 不支持，请在 Excel/WPS 里另存为 .xlsx 或 CSV UTF-8")
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

def norm_key(v):
    """key 比较用：去空格；链接去参数/锚点/末尾斜杠；'123.0' → '123'。"""
    v = str(v or "").strip()
    if not v:
        return ""
    if re.match(r"^https?://", v, re.I):
        v = re.split(r"[?#]", v, 1)[0].rstrip("/")
        scheme, rest = v.split("://", 1)
        host, _, path = rest.partition("/")
        return f"{scheme.lower()}://{host.lower()}/{path}" if path else f"{scheme.lower()}://{host.lower()}"
    if re.fullmatch(r"\d+\.0+", v):
        return v.split(".")[0]
    return v


def do_stack(tables, key, dedupe=True, keep="last"):
    cols = []
    for t in tables:
        for k in all_keys(t):
            if k not in cols:
                cols.append(k)
    if key is None and dedupe:  # 整行去重只比所有表共有的列，避免空串差异
        common_cols = [c for c in cols if all(c in all_keys(t) for t in tables)]
    else:
        common_cols = None
    rows, pos, dropped = [], {}, 0
    for t in tables:
        for r in t:
            if dedupe:
                kv = norm_key(r.get(key, "")) if key else tuple(r.get(c, "") for c in common_cols)
                if not kv or kv == tuple("" for _ in common_cols or []):  # 空 key 不去重，如实保留
                    rows.append(r)
                    continue
                if kv in pos:
                    dropped += 1
                    if keep == "last":  # 新数据覆盖旧数据；新表里空着的格子沿用旧值
                        merged = dict(rows[pos[kv]])
                        merged.update({k: v for k, v in r.items() if v != ""})
                        rows[pos[kv]] = merged
                    continue
                pos[kv] = len(rows)
            rows.append(r)
    return cols, rows, dropped


def do_join(tables, key):
    main_rows = [dict(r) for r in tables[0]]
    main_cols = all_keys(tables[0])
    idx = {}
    for i, r in enumerate(main_rows):
        kv = norm_key(r.get(key, ""))
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
            kv = norm_key(r.get(key, ""))
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
                 dropped, match_stats, out_csv, keep="last"):
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
        lines += ["", f"- 去重合并 {dropped} 行（key 重复，"
                  f"{'保留后出现的较新数据，空格子沿用旧值' if keep == 'last' else '保留先出现的'}）"]
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
    ap.add_argument("--keep", choices=["last", "first"], default="last",
                    help="stack 去重时保留哪一份：last 后出现的（默认，适合按月份合并）/ first 先出现的")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()

    files = collect_files(args.inputs)
    if args.output:
        out_resolved = Path(args.output).expanduser().resolve()
        files = [f for f in files if f.resolve() != out_resolved] or files
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
        cols, rows, dropped = do_stack(tables, key, dedupe=not args.no_dedupe, keep=args.keep)
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
                 dropped, match_stats, out_csv, keep=args.keep)

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
