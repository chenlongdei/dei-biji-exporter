#!/usr/bin/env python3
"""
biji-export skill - Get笔记（biji.com）订阅博主逐字稿批量导出 CLI
还原自 DouClaw v1.0.1（用户自有软件），重构为 agent 友好形态。

子命令:
  login          交互式浏览器授权（首次/换号时用）
  topics         列出账号下所有知识库
  follows        列出某知识库下订阅的博主
  export         导出某博主的逐字稿（Markdown，增量续传）
  status         查看某 URL 对应目录的导出状态与续传建议
  doctor         环境自检（Python/playwright/浏览器/登录态/网络/导出目录），给出下一步该跑的命令

所有子命令 stdout 只输出一个 JSON（--pretty 美化），供 agent 直接解析；
进度写到 stderr。失败时 JSON 带 error_code 和 next_action，退出码非 0。
"""
import argparse
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import auth_helper  # 同目录；也负责把 stdout/stderr 设为 utf-8

API_BASE = os.environ.get("BIJI_API_BASE", "https://knowledge-api.trytalks.com")  # 覆盖仅供离线测试
BIJI_HOME = "https://www.biji.com/"
REQUEST_TIMEOUT = 30
DEFAULT_WORKERS = 3          # 并发下载数：3 已比串行快约 3 倍，又不至于给平台造成压力
MAX_WORKERS = 6
REQUEST_JITTER = 0.3         # 每次详情请求前随机等待 0~0.3 秒
CRED_CACHE = auth_helper.CRED_CACHE
DEFAULT_BASE = Path.home() / "Downloads" / "Get笔记导出"  # 默认下载文件夹；可被 config.json 的 biji_export_base 覆盖


def _apply_base_override():
    """config.json 里配了 biji_export_base 就用它（比如外置素材库）。"""
    global DEFAULT_BASE
    try:
        cfg_path = os.environ.get("BIJI_CONFIG") or (Path(__file__).resolve().parent.parent / "config.json")
        cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        if cfg.get("biji_export_base"):
            DEFAULT_BASE = Path(cfg["biji_export_base"]).expanduser()
    except Exception:
        pass


_apply_base_override()
META_FILE = ".biji_export_meta.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"


# ---------- 凭据 ----------

PY_CMD = "python" if os.name == "nt" else "python3"


class BijiError(RuntimeError):
    """带错误码的业务错误；code 供 agent 分支处理。"""

    def __init__(self, code, message, next_action=None):
        super().__init__(message)
        self.code, self.next_action = code, next_action

    def to_json(self):
        return {"ok": False, "error_code": self.code, "error": str(self),
                "next_action": self.next_action}


def login_action():
    return f"让用户在场，运行 {PY_CMD} run.py login 扫码登录"


def save_credentials(token, csrf):
    auth_helper.save_credentials(token, csrf)


def load_credentials(require=True):
    token, csrf = auth_helper.load_cached_credentials()
    if not token and require:
        raise BijiError("not_logged_in", "还没有登录得到大脑（Get笔记）", login_action())
    return token, csrf


def refresh_token_headless():
    """无头刷新 token。成功返回 (token, csrf)，失败抛 BijiError(token_expired)。"""
    new_token, new_csrf, err = auth_helper.headless_refresh()
    if new_token:
        return new_token, new_csrf
    raise BijiError("token_expired", f"登录已过期且自动刷新失败：{err}", login_action())


def refresh_credentials_if_needed(token=None, csrf=None):
    """JWT 有效期仅 30 分钟。先探活，403 时无头刷新。返回 (token, csrf)。"""
    if token:
        req = urllib.request.Request(f"{API_BASE}/v1/web/subscribe/topic/list?page=1&size=1&exclude_mine=0",
                                     headers=make_headers(token, csrf or ""))
        try:
            with get_opener().open(req, timeout=15):
                return token, csrf  # 仍有效
        except urllib.error.HTTPError as e:
            if e.code != 403:
                return token, csrf  # 非鉴权错误，交给后续请求的重试
        except Exception:
            return token, csrf  # 网络问题先返回原值，后续请求会重试并给出明确网络错误
    return refresh_token_headless()


# ---------- 网络层 ----------

def use_system_proxy() -> bool:
    return auth_helper.use_system_proxy()


def build_opener():
    handlers = []
    if not use_system_proxy():
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


_OPENER = None


def get_opener():
    global _OPENER
    if _OPENER is None:
        _OPENER = build_opener()
    return _OPENER


PROXY_MARKERS = ("tunnel connection failed", "proxy", "unexpected eof while reading")


def explain_network_error(error) -> str:
    text = str(error).lower()
    if any(m in text for m in PROXY_MARKERS):
        return ("网络请求失败，疑似系统代理/VPN/抓包工具导致。请关闭代理软件或取消系统 HTTPS 代理后重试。"
                f"原始错误：{error}")
    return f"网络请求失败：{error}"


RETRY_HTTP_CODES = (429, 500, 502, 503, 504)


def _open_with_retry(req, timeout=REQUEST_TIMEOUT, retries=3):
    """超时/断连/429/5xx 指数退避重试；403 等鉴权错误立即抛出。"""
    last = None
    for attempt in range(retries + 1):
        try:
            return get_opener().open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_HTTP_CODES or attempt == retries:
                raise
            last = e
        except Exception as e:  # 超时/断连
            last = e
        time.sleep(min(8.0, 1.0 * (2 ** attempt)) + random.uniform(0, 0.5))
    raise RuntimeError(f"请求失败（已重试 {retries} 次）：{last}")


def make_headers(token: str, csrf: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "xi-csrf-token": csrf,
        "x-appid": "3",
        "x-av": "1.2.2",
        "x-request-id": str(uuid.uuid4()),
        "Referer": BIJI_HOME,
        "User-Agent": UA,
    }


def post_json(url: str, payload: dict, headers: dict, timeout=REQUEST_TIMEOUT):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with _open_with_retry(req, timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_json(url: str, headers: dict, timeout=REQUEST_TIMEOUT):
    req = urllib.request.Request(url, headers=headers, method="GET")
    with _open_with_retry(req, timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------- API 层 ----------

class ApiError(RuntimeError):
    """接口返回 HTTP 200 但 h.c != 0（限流/参数错误等）。"""


def _unwrap(data):
    """兼容两种响应包裹：{data:{...}} 或 {h:…, c:{...}}。h.c 非 0 视为接口错误。"""
    h = data.get("h") if isinstance(data, dict) else None
    if isinstance(h, dict) and h.get("c") not in (0, None, "0"):
        raise ApiError(f"接口返回错误 c={h.get('c')} e={h.get('e')}")
    if "c" in data and isinstance(data["c"], dict):
        return data["c"]
    if "data" in data and isinstance(data["data"], dict):
        return data["data"]
    return data


def fetch_topics(token, csrf, page_size=50, log=None):
    """获取当前账号可用的知识库列表。"""
    out, page = [], 1
    while True:
        url = f"{API_BASE}/v1/web/subscribe/topic/list?page={page}&size={page_size}&exclude_mine=0"
        data = _unwrap(get_json(url, make_headers(token, csrf)))
        items = data.get("list", []) or []
        out.extend(items)
        if log:
            log(f"  已获取第 {page} 页（累计 {len(out)}）")
        if not data.get("has_more", False) or not items:
            break
        page += 1
    return out


def fetch_follows(topic_alias, token, csrf, page_size=50, log=None):
    """获取某个知识库下订阅的博主列表。"""
    out, page = [], 1
    while True:
        url = (f"{API_BASE}/v1/web/follow/list?topic_id=-1&topic_id_alias={topic_alias}"
               f"&type=1&page={page}&page_size={page_size}")
        data = _unwrap(get_json(url, make_headers(token, csrf)))
        items = data.get("list", []) or []
        out.extend(items)
        if log:
            log(f"  已获取第 {page} 页（累计 {len(out)}）")
        if not data.get("has_next", False) or not items:
            break
        page += 1
    return out


class IncompleteListError(RuntimeError):
    """文章列表没取全（接口抖动/限流）。"""


def fetch_all_posts(topic_id, follow_id, token, csrf, log=None, page_retries=3):
    """获取某博主下所有文章（POST JSON 分页）。
    用接口返回的 total / has_more 判断是否取全；某页意外为空或报错会退避重试，
    最终仍少于 total 则抛 IncompleteListError——绝不把“没取到”当成“没有文章”。"""
    out, seen, page, total = [], set(), 1, None
    while True:
        payload = {"topic_id": topic_id, "follow_id": follow_id, "page": page, "page_size": 50}
        posts, has_more = [], None
        for attempt in range(page_retries + 1):
            try:
                data = _unwrap(post_json(f"{API_BASE}/v1/web/follow/account/posts", payload,
                                         make_headers(token, csrf)))
            except ApiError as e:
                if attempt == page_retries:
                    raise IncompleteListError(f"第 {page} 页文章列表获取失败：{e}")
                time.sleep(1.5 * (attempt + 1))
                continue
            posts = data.get("posts", []) or data.get("list", []) or []
            has_more = data.get("has_more")
            if isinstance(data.get("total"), int):
                total = data["total"]
            expect_more = (has_more is True) or (total is not None and len(out) < total)
            if posts or not expect_more or attempt == page_retries:
                break
            time.sleep(1.5 * (attempt + 1))  # 该有却是空页：多半是限流，退避重试
        for p in posts:
            pid = str(p.get("post_id_str") or p.get("post_id") or "")
            if pid and pid in seen:
                continue
            seen.add(pid)
            out.append(p)
        if log:
            log(f"  已获取第 {page} 页，共 {len(posts)} 篇（累计 {len(out)}{'/' + str(total) if total else ''}）")
        if not posts:
            break
        if has_more is False:
            break
        if has_more is None and len(posts) < 50:  # 老接口没有 has_more 时的兜底
            break
        page += 1
    if total is not None and len(out) < total:
        raise IncompleteListError(f"文章列表只取到 {len(out)}/{total} 篇（接口抖动或限流）")
    return out


def fetch_post_detail(post_id_str, topic_alias, token, csrf, topic_id=None):
    """获取单篇文章详情，返回 post 对象（含 post_media_text 逐字稿）。
    注意：topic_id 必须传真实数字 id；detail 的 c 直接就是 post 对象（不再嵌套 post 键）。"""
    payload = {"topic_id": topic_id, "topic_id_alias": topic_alias,
               "post_id": str(post_id_str), "load_media_text": True}
    data = _unwrap(post_json(f"{API_BASE}/v1/web/topic/post/detail", payload,
                             make_headers(token, csrf)))
    if "post" in data and isinstance(data["post"], dict):
        return data["post"]
    # c 本身就是 post 对象
    return data if data.get("post_id_str") or data.get("post_id") else {}


def fetch_topic_id(topic_alias, token, csrf):
    """通过 alias 获取真实 topic_id。"""
    url = f"{API_BASE}/v1/web/topic/detail?id_alias={topic_alias}&source=web"
    data = _unwrap(get_json(url, make_headers(token, csrf)))
    return data.get("topic_id") or data.get("id")


# ---------- 工具函数 ----------

TIME_FIELD_CANDIDATES = ("post_publish_time", "post_create_time", "post_update_time",
                         "publish_time", "created_at", "create_time")
DATE_FMTS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d")


def parse_api_datetime(value):
    """尽量兼容接口里常见的时间格式。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value)
        except Exception:
            return None
    if isinstance(value, str):
        v = value.replace("+00:00", "").replace("Z", "").replace("T", " ").strip()
        for fmt in DATE_FMTS:
            try:
                return datetime.strptime(v, fmt)
            except ValueError:
                continue
    return None


def extract_post_datetime(post):
    """从文章列表项里提取发布时间。"""
    for key in TIME_FIELD_CANDIDATES:
        dt = parse_api_datetime(post.get(key))
        if dt:
            return dt
    for key, val in post.items():
        if isinstance(val, str) and re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", val):
            dt = parse_api_datetime(val)
            if dt:
                return dt
    return None


def parse_user_date(value, is_end=False):
    """解析用户输入的日期，格式统一为 YYYY-MM-DD。"""
    value = (value or "").strip()
    if not value:
        return None
    v = value[:10].replace("/", "-")
    try:
        d = datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"日期格式无效：{value}，请使用 YYYY-MM-DD")
    # 注意：返回的是 date，比较也按 date；原先的 “+1天-1秒” 对 date 不生效，会让截止日多算一天
    return d


def filter_posts_by_date(posts, start_date, end_date, log=None):
    if start_date and end_date and start_date > end_date:
        raise ValueError("起始时间不能晚于截止时间")
    timed = [(extract_post_datetime(p), p) for p in posts]
    if not any(t for t, _ in timed):
        if log:
            log("⚠️  当前接口未返回可用的文章时间，已自动忽略时间筛选并导出全部文章")
        return posts, 0
    def _as_date(d):
        return d.date() if isinstance(d, datetime) else d

    kept, missing = [], 0
    start_cmp = _as_date(start_date) if start_date else None
    end_cmp = _as_date(end_date) if end_date else None
    for dt, p in timed:
        if dt is None:
            missing += 1
            continue
        d = dt.date() if isinstance(dt, datetime) else dt
        if start_cmp and d < start_cmp:
            continue
        if end_cmp and d > end_cmp:
            continue
        kept.append(p)
    if log:
        log(f"🗓️  时间筛选：{start_date or '不限'} ~ {end_date or '不限'}")
        log(f"✅ 筛选后剩余 {len(kept)} 篇文章")
        if missing:
            log(f"⚠️  有 {missing} 篇文章缺少时间字段，已在筛选时跳过")
    return kept, missing


_RADICALS = re.compile("[\u2e80-\u2eff\u2f00-\u2fdf]")


def fix_lookalikes(text):
    """抖音标题常混入康熙部首“⼯⼈⼥”（长得像汉字，但搜“打工人”搜不到）。
    只规范化这两个区段，不动全角标点。"""
    import unicodedata
    return _RADICALS.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), text or "")


def split_title(raw):
    """多行标题（抖音“标题+简介”）→ (首行标题, 其余简介)。"""
    lines = [l.strip() for l in fix_lookalikes(raw).replace("\r", "").split("\n") if l.strip()]
    if not lines:
        return "", ""
    return lines[0], " ".join(lines[1:])


def sanitize_filename(name, max_len=60):
    """清理文件名中的非法字符并截断（防超系统 255 字节上限）。"""
    name = (name or "").strip()
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = name.strip(". ._")
    return name[:max_len].rstrip() or "未命名"


def parse_url(url):
    """
    从 biji.com 订阅页 URL 解析 topic_id_alias 和 follow_id。
    示例：https://www.biji.com/subject/zJKPkQ4n/DEFAULT?followId=1052194&followName=胡说老王
    """
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)
    m = re.match(r"^/subject/([^/]+)", parsed.path)
    if not m:
        raise ValueError(f"无法解析 URL，请确认格式正确。示例："
                         "https://www.biji.com/subject/zJKPkQ4n/DEFAULT?followId=1052194&followName=xxx")
    qs = parse_qs(parsed.query)
    follow_id = (qs.get("followId") or [None])[0]
    follow_name = (qs.get("followName") or ["未命名博主"])[0]
    return m.group(1), follow_id, follow_name


def build_export_identity(url):
    topic_alias, follow_id, follow_name = parse_url(url)
    return topic_alias, follow_id, follow_name


def get_follow_name_from_url(url):
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(url).query)
    return (qs.get("followName") or ["未命名博主"])[0]


def build_follow_url(topic_alias, follow_id, follow_name):
    from urllib.parse import quote
    return (f"https://www.biji.com/subject/{topic_alias}/DEFAULT"
            f"?followId={follow_id}&followName={quote(str(follow_name))}")


# ---------- 导出目录与增量状态 ----------

def read_export_meta(output_dir):
    p = Path(output_dir) / META_FILE
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_export_meta(output_dir, url):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    meta = read_export_meta(output_dir)
    meta.update({"url": url, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    (output_dir / META_FILE).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def find_existing_output_dir(base_dir, url):
    """在 base_dir 下找历史导出目录（按 url / identity 匹配）。"""
    topic_alias, follow_id, follow_name = build_export_identity(url)
    base = Path(base_dir)
    if not base.is_dir():
        return None
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        meta = read_export_meta(d)
        if meta.get("url") == url:
            return d
    # 兜底：按目录命名约定匹配
    pat = re.compile(rf"{re.escape(sanitize_filename(follow_name))}_.*_{re.escape(str(follow_id))}$")
    for d in sorted(base.iterdir()):
        if d.is_dir() and (pat.match(d.name) or follow_id in d.name.split("_")):
            return d
    return None


def default_export_base_dir():
    return str(DEFAULT_BASE)


def resolve_output_dir(url, output_dir, output_is_base):
    if output_dir:
        output_dir = Path(output_dir).expanduser()
        if output_is_base:
            hit = find_existing_output_dir(output_dir, url)
            if hit:
                return Path(hit), True
            _, follow_id, follow_name = build_export_identity(url)
            return output_dir / f"{sanitize_filename(follow_name)}_{follow_id}", False
        return output_dir, False
    base = Path(default_export_base_dir())
    hit = find_existing_output_dir(base, url)
    if hit:
        return Path(hit), True
    _, follow_id, follow_name = build_export_identity(url)
    return base / f"{sanitize_filename(follow_name)}_{follow_id}", False


def list_exported_post_indexes(output_dir):
    """扫描 {index:03d}_{post_id}.md，返回 {post_id_str: index}。"""
    out = {}
    p = Path(output_dir)
    if not p.is_dir():
        return out
    for f in p.glob("*.md"):
        # 文件名格式：{序号:03d}_{post_id}_{标题}.md
        m = re.match(r"^(\d+)_(\d+)_.*\.md$", f.name) or re.match(r"^(\d+)_.*\.md$", f.name)
        if m:
            if len(m.groups()) == 2:
                out[m.group(2)] = int(m.group(1))
            else:
                # 旧格式无 post_id 段，跳过（无法精确匹配）
                pass
    return out


def build_resume_suggestion(url, token, csrf, output_dir=None, output_is_base=False):
    """给出续传建议：start_date 从缺的最小日期开始。"""
    try:
        topic_alias, follow_id, follow_name = build_export_identity(url)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if follow_id is not None:
        try:
            follow_id = int(follow_id)
        except (TypeError, ValueError):
            pass
    final_dir, _ = resolve_output_dir(url, output_dir, output_is_base)
    exported = list_exported_post_indexes(final_dir)
    if not exported:
        return {"ok": True, "output_dir": str(final_dir), "start_date": None,
                "exported_count": 0, "matched_count": 0,
                "message": "当前目录还没有已导出的文件"}
    try:
        topic_id = fetch_topic_id(topic_alias, token, csrf)
        posts = fetch_all_posts(topic_id, follow_id, token, csrf)
    except Exception:
        return {"ok": True, "output_dir": str(final_dir), "start_date": None,
                "exported_count": len(exported), "matched_count": 0,
                "message": "已找到导出文件，但暂时无法联网核对，可运行 export 增量补齐"}
    matched = 0
    missing_dates = []
    unexported = 0
    for post in posts:
        pid = str(post.get("post_id_str") or post.get("post_id") or "")
        if not pid:
            continue
        dt = extract_post_datetime(post)
        if pid in exported:
            matched += 1
        else:
            unexported += 1
            if dt:
                missing_dates.append(dt.date())
    if missing_dates:
        missing_dates.sort()
        return {"ok": True, "output_dir": str(final_dir),
                "start_date": missing_dates[0].strftime("%Y-%m-%d"),
                "exported_count": len(exported), "matched_count": matched,
                "missing_count": len(missing_dates),
                "message": f"已导出 {len(exported)} 篇，还差 {unexported} 篇；建议从 {missing_dates[0]} 开始补导（export 会自动跳过已有）"}
    if unexported > 0:
        return {"ok": True, "output_dir": str(final_dir), "start_date": None,
                "exported_count": len(exported), "matched_count": matched,
                "unexported_count": unexported,
                "message": f"已导出 {len(exported)} 篇，还有 {unexported} 篇未导出（无日期信息）；直接 export 即可增量补齐"}
    return {"ok": True, "output_dir": str(final_dir), "start_date": None,
            "exported_count": len(exported), "matched_count": matched,
            "message": "全部文章均已导出，无需续传"}


# ---------- 主导出流程 ----------

def export(url, token, csrf, output_dir=None, start_date=None, end_date=None,
           output_is_base=False, on_progress=None, max_items=None, workers=DEFAULT_WORKERS):
    """
    导出主流程。返回摘要 dict。
    on_progress(current_index, total, filename, status)；status 为 None 时 filename 位置是日志文本。
    - 并发 workers 个请求取详情，按原顺序落盘（编号稳定）
    - 中途 403：自动无头刷新 token 一次后继续；刷新失败则停下并返回 ok=False（已导的保留，重跑续传）
    - 无论成功失败，links.json / 元数据 / 空稿清单都会写盘
    """
    def log(msg):
        if on_progress:
            on_progress(None, None, None, msg)

    workers = max(1, min(int(workers or 1), MAX_WORKERS))
    try:
        topic_alias, follow_id, follow_name = build_export_identity(url)
    except ValueError:
        log("❌ 无法解析 URL，请确认格式正确")
        raise

    log(f"🔍 正在获取知识库信息（alias={topic_alias}）...")
    topic_id = fetch_topic_id(topic_alias, token, csrf)
    if not topic_id:
        raise RuntimeError("获取 topic_id 失败，请检查 token 是否有效")
    if follow_id is not None:
        try:
            follow_id = int(follow_id)
        except (TypeError, ValueError):
            pass
    log(f"✅ topic_id = {topic_id}，follow_id = {follow_id}")

    log("📋 正在获取文章列表...")
    posts = fetch_all_posts(topic_id, follow_id, token, csrf)
    log(f"✅ 共找到 {len(posts)} 篇文章")
    if not posts:
        raise IncompleteListError("文章列表为空：可能是接口限流，或该博主在 Get笔记 里确实还没有内容")

    start_d = parse_user_date(start_date)
    end_d = parse_user_date(end_date, is_end=True)
    posts, _missing = filter_posts_by_date(posts, start_d, end_d, log=log)

    final_dir, resumed = resolve_output_dir(url, output_dir, output_is_base)
    log(f"📂 {'检测到历史目录，继续写入' if resumed else '首次导出，将创建目录'}：{final_dir}")
    final_dir.mkdir(parents=True, exist_ok=True)

    exported_idx = list_exported_post_indexes(final_dir)
    next_index = max(exported_idx.values(), default=0) + 1

    # post_id → 抖音源链接索引（评论区抓取/数据匹配用）
    links_path = final_dir / "links.json"
    links = {}
    try:
        links = json.loads(links_path.read_text(encoding="utf-8"))
    except Exception:
        pass

    def _clean_url(u):
        return (u or "").split("?")[0] if u else ""

    creds = {"token": token, "csrf": csrf, "refreshed": 0}
    creds_lock = threading.Lock()

    def _detail(pid):
        """取详情；403 时（整个进程内）最多无头刷新 2 次 token。返回 (detail, err)。"""
        for _ in range(3):
            used = creds["token"]
            try:
                time.sleep(random.uniform(0, REQUEST_JITTER))
                return fetch_post_detail(pid, topic_alias, creds["token"], creds["csrf"],
                                         topic_id=topic_id), None
            except urllib.error.HTTPError as e:
                if e.code != 403:
                    return None, f"http_{e.code}"
                with creds_lock:
                    if creds["token"] == used:  # 别的线程还没刷新过
                        if creds["refreshed"] >= 2:
                            return None, "auth"
                        log("🔑 token 已过期，正在自动刷新（无需操作）...")
                        try:
                            creds["token"], creds["csrf"] = refresh_token_headless()
                            creds["refreshed"] += 1
                            log("🔑 token 刷新成功，继续导出")
                        except BijiError as be:
                            creds["auth_error"] = str(be)
                            return None, "auth"
            except Exception:
                return None, "error"
        return None, "auth"

    # 1) 分出：已导出 / 待导出
    todo, backfill, skip_exist = [], [], 0
    for i, post in enumerate(posts, 1):
        pid = str(post.get("post_id_str") or post.get("post_id") or "")
        if pid and pid in exported_idx:
            skip_exist += 1
            if pid not in links:
                backfill.append(pid)
            continue
        todo.append((i, pid, post))
    log(f"📝 已导出 {skip_exist} 篇，本次待导出 {len(todo)} 篇（并发 {workers}）")

    ok_count, skip_empty, fail_count = 0, 0, 0
    empties, failed_ids = [], []
    auth_dead = False
    total = len(posts)
    attempted = 0
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            # 旧导出缺链接的回填
            if backfill:
                for pid, (d, err) in zip(backfill, ex.map(_detail, backfill)):
                    u = _clean_url((d or {}).get("post_url"))
                    if u:
                        links[pid] = u
                        _backfill_link_into_md(final_dir, pid, u)
            chunk = workers * 4
            for s in range(0, len(todo), chunk):
                batch = todo[s:s + chunk]
                if max_items:
                    batch = batch[:max(0, max_items - ok_count)]  # 空稿不计数，可能需要下一批补
                    if not batch:
                        break
                results = list(ex.map(lambda t: _detail(t[1]), batch))
                for (i, pid, post), (detail, err) in zip(batch, results):
                    attempted += 1
                    name = sanitize_filename(post.get("post_name") or f"未命名_{i:03d}")
                    if err:
                        if err == "auth":
                            auth_dead = True
                        fail_count += 1
                        failed_ids.append(pid)
                        if on_progress:
                            on_progress(i, total, name, err)
                        continue
                    text = detail.get("post_media_text") or ""
                    title_raw = detail.get("post_name") or post.get("post_name") or f"未命名_{i:03d}"
                    title, intro = split_title(title_raw)
                    title = title or f"未命名_{i:03d}"
                    source_url = _clean_url(detail.get("post_url"))
                    if source_url and pid:
                        links[pid] = source_url
                    if not text.strip():
                        skip_empty += 1
                        empties.append((pid, title, source_url))
                        if on_progress:
                            on_progress(i, total, name, "empty")
                        continue
                    pub = extract_post_datetime(post)
                    header = [f"# {title}"]
                    if intro:
                        header.append(f"> 简介：{intro}")
                    if pub:
                        header.append(f"> 发布时间：{pub.strftime('%Y-%m-%d %H:%M')}")
                    if source_url:
                        header.append(f"> 链接：{source_url}")
                    content = "\n\n".join(header) + "\n\n" + fix_lookalikes(text.strip()) + "\n"
                    out_file = final_dir / f"{next_index:03d}_{pid}_{sanitize_filename(title)}.md"
                    out_file.write_text(content, encoding="utf-8")
                    if pid:
                        exported_idx[pid] = next_index
                    next_index += 1
                    ok_count += 1
                    if on_progress:
                        on_progress(i, total, out_file.name, "ok")
                    if max_items and ok_count >= max_items:
                        break
                if auth_dead:
                    log("❌ token 刷新失败，已停止；已导出的文件都保留，重新登录后重跑会自动续传")
                    break
                if max_items and ok_count >= max_items:
                    log(f"⏹️  已达到单次上限 {max_items} 篇，下次运行自动续传")
                    break
    finally:
        try:
            links_path.write_text(json.dumps(links, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
        write_export_meta(final_dir, url)
        _write_empty_list(final_dir, empties)

    remaining = len(todo) - attempted + fail_count
    elapsed = time.time() - t0
    log(f"🎉 本次成功 {ok_count} 篇，跳过已存在 {skip_exist} 篇，空稿 {skip_empty} 篇，失败 {fail_count} 篇，"
        f"用时 {elapsed:.0f} 秒")
    log(f"📁 输出目录：{final_dir}")

    complete = remaining - fail_count == 0 and fail_count == 0
    summary = {
        "ok": not auth_dead,
        "complete": complete and not auth_dead,
        "output_dir": str(final_dir),
        "follow_name": follow_name,
        "total_found": total,
        "exported": ok_count,
        "skipped_existing": skip_exist,
        "skipped_empty": skip_empty,
        "failed": fail_count,
        "remaining": remaining,
        "seconds": round(elapsed, 1),
        "sec_per_item": round(elapsed / attempted, 2) if attempted else None,
    }
    if auth_dead:
        summary.update({"error_code": "token_expired",
                        "error": creds.get("auth_error") or "登录已过期",
                        "next_action": login_action() + "，然后重跑同一条导出命令（自动续传）"})
    elif not complete:
        summary["next_action"] = (f"还有 {remaining} 篇没导（失败 {fail_count} 篇）："
                                  "重跑同一条命令即可自动续传，直到 complete=true")
    else:
        summary["next_action"] = None
    if empties:
        summary["empty_list"] = str(final_dir / EMPTY_LIST_FILE)
    return summary


EMPTY_LIST_FILE = "_空稿清单.md"


def _write_empty_list(final_dir, empties):
    """没有逐字稿的视频不生成 md，但记下标题和链接，免得用户以为漏了。"""
    p = Path(final_dir) / EMPTY_LIST_FILE
    if not empties:
        return
    lines = ["# 没有逐字稿的视频", "",
             "以下视频在 Get笔记 里暂时没有逐字稿（可能是纯音乐/图文/还没转写完），所以没生成 md。",
             "过几天重跑导出会自动再检查一次。", "", "| post_id | 标题 | 链接 |", "| --- | --- | --- |"]
    for pid, title, u in empties:
        lines.append(f"| {pid} | {str(title).replace('|', '｜').replace(chr(10), ' ')} | {u} |")
    try:
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def _backfill_link_into_md(final_dir, pid, u):
    for f in Path(final_dir).glob(f"*_{pid}_*.md"):
        try:
            txt = f.read_text(encoding="utf-8")
            if "> 链接：" in txt[:600]:
                continue
            lines = txt.split("\n")
            for li, line in enumerate(lines[:5]):
                if line.startswith(">"):
                    lines.insert(li, f"> 链接：{u}")
                    break
            f.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass


# ---------- CLI 子命令 ----------

def emit(obj, pretty=False):
    print(json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None), flush=True)


def eprint(msg):
    print(msg, file=sys.stderr, flush=True)


def network_error_json(e):
    return {"ok": False, "error_code": "network", "error": explain_network_error(e),
            "next_action": "检查网络；开了 VPN/代理就先关掉再重试，确需代理时设置环境变量 BIJI_USE_SYSTEM_PROXY=1"}


def http_error_json(e):
    if e.code == 403:
        return BijiError("token_expired", "登录已过期（HTTP 403）", login_action()).to_json()
    return {"ok": False, "error_code": f"http_{e.code}", "error": f"HTTP {e.code}",
            "next_action": "稍后重试；持续出现请截图反馈"}


def run_guarded(fn, args):
    """统一异常 → JSON + 非 0 退出码。"""
    try:
        return fn(args)
    except BijiError as e:
        emit(e.to_json(), getattr(args, "pretty", False))
        return 2
    except urllib.error.HTTPError as e:
        emit(http_error_json(e), getattr(args, "pretty", False))
        return 2
    except ValueError as e:
        emit({"ok": False, "error_code": "bad_input", "error": str(e)}, getattr(args, "pretty", False))
        return 2
    except IncompleteListError as e:
        emit({"ok": False, "error_code": "list_incomplete", "error": str(e),
              "next_action": "等 1~2 分钟后重跑同一命令（已导出的不会重复导）"}, getattr(args, "pretty", False))
        return 2
    except (urllib.error.URLError, OSError) as e:
        emit(network_error_json(e), getattr(args, "pretty", False))
        return 2
    except RuntimeError as e:
        if str(e).startswith("请求失败"):
            emit(network_error_json(e), getattr(args, "pretty", False))
        else:
            emit({"ok": False, "error_code": "error", "error": str(e)}, getattr(args, "pretty", False))
        return 2
    except Exception as e:  # 代码 bug：别伪装成网络问题
        import traceback
        emit({"ok": False, "error_code": "internal", "error": f"{type(e).__name__}: {e}",
              "trace": traceback.format_exc()[-800:],
              "next_action": "这是工具本身的 bug，请把这段输出发给作者"}, getattr(args, "pretty", False))
        return 2


def cmd_login(args):
    try:
        auth_helper.extract_token_interactive(force_reauth=args.force)
    except RuntimeError as e:
        emit({"ok": False, "error_code": "login_failed", "error": str(e),
              "next_action": f"按 error 提示处理后重试；不确定就先跑 {PY_CMD} run.py doctor"})
        return 2
    emit({"ok": True, "hint": "凭据已缓存，可直接使用 export-biji / topics / follows"})
    return 0


def list_topics(token, csrf):
    topics = fetch_topics(token, csrf)
    return [{"alias": t.get("id_alias") or t.get("alias"), "name": t.get("name", "未命名知识库"),
             "follow_count": ((t.get("extend_data") or {}).get("stats") or {}).get("follow_count")
             or ((t.get("stats_info") or {}).get("follow_count"))}
            for t in topics]


def list_follows(alias, token, csrf):
    items = []
    for f in fetch_follows(alias, token, csrf):
        fid = f.get("id") or f.get("follow_id")
        name = f.get("name", "未命名博主")
        items.append({"name": name, "platform": f.get("platform"),
                      "post_count": (f.get("extend_data") or {}).get("get_note_count") or f.get("post_count"),
                      "follow_id": fid, "topic_alias": alias,
                      "url": build_follow_url(alias, fid, name)})
    return items


def cmd_topics(args):
    token, csrf = refresh_credentials_if_needed(*load_credentials())
    items = list_topics(token, csrf)
    emit({"ok": True, "count": len(items), "items": items}, args.pretty)
    return 0


def cmd_follows(args):
    token, csrf = refresh_credentials_if_needed(*load_credentials())
    items = list_follows(args.topic_alias, token, csrf)
    emit({"ok": True, "count": len(items), "items": items}, args.pretty)
    return 0


def _norm_name(s):
    return re.sub(r"\s+", "", str(s or "")).casefold()


def find_blogger(name, token, csrf):
    """按名字在所有知识库里找博主：完全同名优先，其次包含匹配。
    返回 (唯一命中 or None, 候选列表)。"""
    want = _norm_name(name)
    exact, partial = [], []
    for t in list_topics(token, csrf):
        for f in list_follows(t["alias"], token, csrf):
            f["topic_name"] = t["name"]
            n = _norm_name(f["name"])
            if n == want:
                exact.append(f)
            elif want and want in n:
                partial.append(f)
    if len(exact) == 1:
        return exact[0], exact
    cands = exact or partial
    if len(cands) == 1:
        return cands[0], cands
    return None, cands


def cmd_find(args):
    token, csrf = refresh_credentials_if_needed(*load_credentials())
    hit, cands = find_blogger(args.name, token, csrf)
    if hit:
        emit({"ok": True, "match": hit}, args.pretty)
        return 0
    if cands:
        emit({"ok": False, "error_code": "ambiguous",
              "error": f"名字含「{args.name}」的博主有 {len(cands)} 个，不确定是哪个",
              "candidates": [{"name": c["name"], "topic": c["topic_name"],
                              "post_count": c["post_count"], "url": c["url"]} for c in cands[:20]],
              "next_action": "把候选名单给用户选，然后用完整名字重跑，或 export-biji --url <候选的url>"},
             args.pretty)
        return 3
    emit({"ok": False, "error_code": "not_found",
          "error": f"所有知识库里都没有名字含「{args.name}」的博主",
          "next_action": f"确认用户在 Get笔记 订阅了该博主；可跑 {PY_CMD} biji_export.py topics / follows <alias> 列出来让用户认"},
         args.pretty)
    return 3


def _progress_printer(total_hint=None):
    """进度写 stderr：日志行全打；逐篇只在失败/空稿和每 20 篇打一次，避免撑爆 agent 上下文。"""
    state = {"ok": 0, "t0": time.time()}

    def on_progress(idx, total, fname, status):
        if idx is None:  # 日志行
            eprint(status)
            return
        if status == "ok":
            state["ok"] += 1
            if state["ok"] % 20 == 0:
                rate = (time.time() - state["t0"]) / state["ok"]
                eprint(f"  … 已导出 {state['ok']} 篇（第 {idx}/{total} 篇，约 {rate:.1f} 秒/篇）")
        elif status != "skip":
            eprint(f"  [{idx}/{total}] {status}: {fname}")
    return on_progress


def cmd_export(args):
    token, csrf = refresh_credentials_if_needed(*load_credentials())
    if args.dry_run:
        sug = build_resume_suggestion(args.url, token, csrf, args.output, args.output_is_base)
        emit({"ok": True, "dry_run": True, **sug}, args.pretty)
        return 0
    summary = export(args.url, token, csrf, output_dir=args.output,
                     start_date=args.start_date, end_date=args.end_date,
                     output_is_base=args.output_is_base,
                     on_progress=_progress_printer(),
                     max_items=args.max_items, workers=args.workers)
    emit(summary, args.pretty)
    return 0 if summary.get("ok") else 2


def cmd_status(args):
    token, csrf = load_credentials(require=False)
    sug = build_resume_suggestion(args.url, token, csrf, args.output, args.output_is_base)
    emit(sug, args.pretty)
    return 0 if sug.get("ok") else 2


def cmd_doctor(args):
    """环境自检：每项 {name, ok, detail, fix}；next_action = 第一个失败项的修法。"""
    checks = []

    def add(name, ok, detail, fix=None):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "fix": None if ok else fix})

    import platform
    v = sys.version_info
    add("python", v >= (3, 9), f"{platform.python_version()} @ {sys.executable}",
        "需要 Python ≥ 3.9：macOS 跑 xcode-select --install 或装 python.org 版本；Windows 从 python.org 装并勾选 Add to PATH")
    add("system", True, f"{platform.system()} {platform.release()} {platform.machine()}")

    pw_ok = False
    try:
        import importlib.metadata as md
        add("playwright", True, f"已安装 {md.version('playwright')}")
        pw_ok = True
    except Exception:
        add("playwright", False, "未安装（只用表格合并可以忽略）",
            f"{PY_CMD} -m pip install --user playwright -i https://pypi.tuna.tsinghua.edu.cn/simple")

    if pw_ok and not args.skip_browser:
        found, errs = None, []
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                for ch in auth_helper.browser_order():
                    try:
                        kw = {"headless": True}
                        if ch:
                            kw["channel"] = ch
                        b = p.chromium.launch(**kw)
                        found = f"{ch or 'chromium(自带)'} {b.version}"
                        b.close()
                        break
                    except Exception as e:
                        errs.append(f"{ch or 'chromium'}: {str(e).strip().splitlines()[0][:100]}")
        except Exception as e:
            errs.append(str(e)[:200])
        add("browser", found, found or "; ".join(errs),
            "装 Google Chrome（推荐，不用额外下载），或 "
            f"{PY_CMD} -m playwright install chromium")

    token, csrf = load_credentials(require=False)
    if not token:
        add("login", False, "还没登录", login_action())
    else:
        try:
            req = urllib.request.Request(f"{API_BASE}/v1/web/subscribe/topic/list?page=1&size=1&exclude_mine=0",
                                         headers=make_headers(token, csrf or ""))
            with get_opener().open(req, timeout=15):
                add("login", True, f"token 有效（{auth_helper.CRED_CACHE}）")
        except urllib.error.HTTPError as e:
            if e.code == 403:
                add("login", True, "token 已过期，导出时会自动无头刷新（刷新失败才需要重新 login）")
            else:
                add("network", False, f"HTTP {e.code}", "稍后重试")
        except Exception as e:
            add("network", False, explain_network_error(e),
                "关闭 VPN/代理后重试；确需代理设置 BIJI_USE_SYSTEM_PROXY=1")

    base = Path(default_export_base_dir())
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("export_dir", True, str(base))
    except Exception as e:
        add("export_dir", False, f"{base} 不可写：{e}", "换一个导出目录写进 config.json 的 biji_export_base")

    proxies = urllib.request.getproxies()
    if proxies:
        checks.append({"name": "proxy", "ok": True,
                       "detail": f"检测到系统代理 {proxies}；脚本默认绕过代理直连"
                                 f"{'（当前设置为走代理）' if use_system_proxy() else ''}", "fix": None})

    bad = [c for c in checks if not c["ok"]]
    emit({"ok": not bad, "checks": checks,
          "next_action": bad[0]["fix"] if bad else None,
          "merge_tables_ready": v >= (3, 9),
          "export_ready": not bad}, True)
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser(description="Get笔记订阅博主逐字稿导出 CLI（biji-export skill）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="交互式浏览器授权")
    p.add_argument("--force", action="store_true", help="切换账号（要求退出旧号）")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("topics", help="列出知识库")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_topics)

    p = sub.add_parser("follows", help="列出某知识库下的博主")
    p.add_argument("topic_alias")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_follows)

    p = sub.add_parser("find", help="按名字找博主（完全同名优先；多个候选时列出来）")
    p.add_argument("name")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("export", help="导出博主逐字稿")
    p.add_argument("url", help="博主订阅页 URL（用 follows/find 查到的 url）")
    p.add_argument("--output", "-o", help="输出目录（配合 --output-is-base 作为根目录）")
    p.add_argument("--output-is-base", action="store_true", help="把 --output 当根目录，自动建子目录")
    p.add_argument("--start-date", help="YYYY-MM-DD")
    p.add_argument("--end-date", help="YYYY-MM-DD")
    p.add_argument("--max-items", type=int, help="单次最多导出篇数")
    p.add_argument("--workers", type=int, default=int(os.environ.get("BIJI_WORKERS", DEFAULT_WORKERS)),
                   help=f"并发数（默认 {DEFAULT_WORKERS}，上限 {MAX_WORKERS}）")
    p.add_argument("--dry-run", action="store_true", help="只看会导出什么，不落盘")
    p.add_argument("--verbose", "-v", action="store_true", help="（兼容旧参数，进度现在默认输出到 stderr）")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("status", help="查看导出状态与续传建议")
    p.add_argument("url")
    p.add_argument("--output", "-o")
    p.add_argument("--output-is-base", action="store_true")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="环境自检，告诉你下一步该跑什么")
    p.add_argument("--skip-browser", action="store_true", help="跳过浏览器启动检测（更快）")
    p.set_defaults(func=cmd_doctor)

    args = ap.parse_args()
    if args.cmd in ("login", "doctor"):
        sys.exit(args.func(args) or 0)
    sys.exit(run_guarded(args.func, args) or 0)


if __name__ == "__main__":
    main()
