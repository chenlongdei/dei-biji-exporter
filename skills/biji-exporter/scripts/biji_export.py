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

所有子命令输出 JSON（--pretty 美化），供 agent 直接解析。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path

API_BASE = "https://knowledge-api.trytalks.com"
BIJI_HOME = "https://www.biji.com/"
REQUEST_TIMEOUT = 30
CRED_CACHE = Path.home() / ".biji_exporter" / "skill_credentials.json"
DEFAULT_BASE = Path.home() / "Downloads" / "Get笔记导出"  # 默认下载文件夹；可被 config.json 的 biji_export_base 覆盖


def _apply_base_override():
    """config.json 里配了 biji_export_base 就用它（比如外置素材库）。"""
    global DEFAULT_BASE
    try:
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
        if cfg.get("biji_export_base"):
            DEFAULT_BASE = Path(cfg["biji_export_base"]).expanduser()
    except Exception:
        pass


_apply_base_override()
META_FILE = ".biji_export_meta.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"


# ---------- 凭据 ----------

def save_credentials(token, csrf):
    """把 token/csrf 缓存到磁盘，供 CLI 免登录复用。"""
    CRED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CRED_CACHE.write_text(
        json.dumps({"token": token, "csrf": csrf,
                    "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
        encoding="utf-8")


def load_credentials(require=True):
    try:
        data = json.loads(CRED_CACHE.read_text(encoding="utf-8"))
        return data.get("token"), data.get("csrf")
    except Exception:
        if require:
            print(json.dumps({"ok": False, "error": "not_logged_in",
                              "hint": "先运行 biji_export.py login 完成授权"}, ensure_ascii=False))
            sys.exit(2)
        return None, None


def refresh_credentials_if_needed(token=None, csrf=None):
    """JWT 有效期仅 30 分钟。失效（403/LoginRequired）时无头刷新：
    用 skill 专属登录态目录起 headless chromium 重新读取 token/csrf。
    返回 (token, csrf)；刷新失败抛 RuntimeError。"""
    # 先验证现有 token（走统一 opener，代理行为一致）
    if token:
        req = urllib.request.Request(f"{API_BASE}/v1/web/subscribe/topic/list?page=1&size=1&exclude_mine=0",
                                     headers=make_headers(token, csrf or ""))
        try:
            with get_opener().open(req, timeout=15):
                return token, csrf  # 仍有效
        except urllib.error.HTTPError as e:
            if e.code != 403:
                return token, csrf  # 非鉴权错误，不处理
        except Exception:
            pass  # 网络问题也先返回原值，后续请求会重试

    # 无头刷新
    new_token, new_csrf = _headless_refresh()
    if new_token:
        save_credentials(new_token, new_csrf)
        return new_token, new_csrf
    raise RuntimeError("token 已过期且自动刷新失败，请运行 login 重新授权")


def _headless_refresh():
    """从 skill 登录态目录无头提取凭据。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None
    import os
    from pathlib import Path
    data_dir = os.environ.get("BIJI_BROWSER_DATA",
                              str(Path.home() / ".biji_exporter" / "browser_data_skill"))
    browsers = os.environ.get("PLAYWRIGHT_BROWSERS_PATH",
                              str(Path.home() / "Library/Application Support/DouClaw/playwright-browsers"))
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers
    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                data_dir, headless=True, args=["--no-proxy-server"])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.biji.com", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            token = page.evaluate("() => localStorage.getItem('token') || ''")
            csrf = page.evaluate(
                "() => { const m = document.cookie.match(/csrfToken=([^;]+)/); return m ? m[1] : ''; }")
            ctx.close()
            return token or None, csrf or ""
    except Exception:
        return None, None


# ---------- 网络层 ----------

def use_system_proxy() -> bool:
    return os.environ.get("BIJI_USE_SYSTEM_PROXY", "").lower() in ("1", "true", "yes")


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


def _open_with_retry(req, timeout=REQUEST_TIMEOUT, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            return get_opener().open(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except Exception as e:  # 超时/断连
            last = e
            time.sleep(1.5)
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

def _unwrap(data):
    """兼容两种响应包裹：{data:{...}} 或 {h:…, c:{...}}。"""
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


def fetch_all_posts(topic_id, follow_id, token, csrf, log=None):
    """获取某博主下所有文章的 post_id_str 和 post_name（POST JSON 分页）。"""
    out, page = [], 1
    while True:
        payload = {"topic_id": topic_id, "follow_id": follow_id,
                   "page": page, "page_size": 50}
        data = _unwrap(post_json(f"{API_BASE}/v1/web/follow/account/posts", payload,
                                 make_headers(token, csrf)))
        posts = data.get("posts", []) or data.get("list", []) or []
        out.extend(posts)
        if log:
            log(f"  已获取第 {page} 页，共 {len(posts)} 篇（累计 {len(out)}）")
        if not posts or len(posts) < 50:
            break
        page += 1
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
    if is_end:
        d = d + timedelta(days=1) - timedelta(seconds=1)
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
           output_is_base=False, on_progress=None, max_items=None):
    """
    导出主流程。返回摘要 dict。
    on_progress(current_index, total, filename, status)
    """
    def log(msg):
        if on_progress:
            on_progress(None, None, None, msg)

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

    start_d = parse_user_date(start_date)
    end_d = parse_user_date(end_date, is_end=True)
    # parse_user_date 返回 date 对象，filter 内部已做 datetime/date 兼容
    posts, _missing = filter_posts_by_date(posts, start_d, end_d, log=log)

    final_dir, resumed = resolve_output_dir(url, output_dir, output_is_base)
    if resumed:
        log(f"📂 检测到历史目录，继续写入：{final_dir}")
    else:
        log(f"📂 首次导出，将创建目录：{final_dir}")
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

    ok_count, skip_exist, skip_empty, fail_count = 0, 0, 0, 0
    total = len(posts)
    for i, post in enumerate(posts, 1):
        pid = str(post.get("post_id_str") or post.get("post_id") or "")
        name = sanitize_filename(post.get("post_name") or f"未命名_{i:03d}")
        filename = f"{next_index:03d}_{pid}_{name}.md" if pid not in exported_idx else None

        if pid and pid in exported_idx:
            skip_exist += 1
            # 回填：旧导出缺链接的，补 fetch 一次 detail 拿 post_url
            if pid not in links:
                try:
                    d = fetch_post_detail(pid, topic_alias, token, csrf, topic_id=topic_id)
                    u = _clean_url(d.get("post_url"))
                    if u:
                        links[pid] = u
                        for f in final_dir.glob(f"*_{pid}_*.md"):
                            txt = f.read_text(encoding="utf-8")
                            if "链接：" not in txt.split("\n\n", 2)[1 if "\n\n" in txt else 0]:
                                lines = txt.split("\n")
                                for li, line in enumerate(lines[:5]):
                                    if line.startswith(">"):
                                        lines.insert(li, f"> 链接：{u}")
                                        break
                                f.write_text("\n".join(lines), encoding="utf-8")
                except Exception:
                    pass
            if on_progress:
                on_progress(i, total, f"{exported_idx[pid]:03d}_{name}.md", "skip")
            continue

        try:
            detail = fetch_post_detail(pid, topic_alias, token, csrf, topic_id=topic_id)
        except urllib.error.HTTPError as e:
            if e.code == 403:
                raise RuntimeError("❌ Token 已过期（403 Forbidden）！请重新运行 login。")
            fail_count += 1
            if on_progress:
                on_progress(i, total, filename, f"http_{e.code}")
            continue
        except Exception:
            fail_count += 1
            if on_progress:
                on_progress(i, total, filename, "error")
            continue

        text = detail.get("post_media_text") or ""
        if not text.strip():
            skip_empty += 1
            if on_progress:
                on_progress(i, total, filename, "empty")
            continue

        title = detail.get("post_name") or post.get("post_name") or f"未命名_{i:03d}"
        pub = extract_post_datetime(post)
        source_url = _clean_url(detail.get("post_url"))
        if source_url and pid:
            links[pid] = source_url
        header = [f"# {title}"]
        if pub:
            header.append(f"> 发布时间：{pub.strftime('%Y-%m-%d %H:%M')}")
        if source_url:
            header.append(f"> 链接：{source_url}")
        content = "\n\n".join(header) + "\n\n" + text.strip() + "\n"
        out_file = final_dir / f"{next_index:03d}_{pid}_{sanitize_filename(title)}.md"
        out_file.write_text(content, encoding="utf-8")
        if pid:
            exported_idx[pid] = next_index
        next_index += 1
        ok_count += 1
        if on_progress:
            on_progress(i, total, out_file.name, "ok")
        if max_items and ok_count >= max_items:
            log(f"⏹️  已达到单次上限 {max_items} 篇，下次运行自动续传")
            break

    try:
        links_path.write_text(json.dumps(links, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    write_export_meta(final_dir, url)
    log(f"🎉 导出完成！成功 {ok_count} 篇，跳过已存在 {skip_exist} 篇，空稿 {skip_empty} 篇，失败 {fail_count} 篇")
    log(f"📁 输出目录：{final_dir}")

    return {
        "ok": True,
        "output_dir": str(final_dir),
        "follow_name": follow_name,
        "total_found": total,
        "exported": ok_count,
        "skipped_existing": skip_exist,
        "skipped_empty": skip_empty,
        "failed": fail_count,
        "next_run_hint": "再次运行相同 URL 会自动跳过已导出文件" if ok_count else None,
    }


# ---------- CLI 子命令 ----------

def emit(obj, pretty=False):
    print(json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None))


def cmd_login(args):
    from auth_helper import extract_token_interactive
    token, csrf = extract_token_interactive(force_reauth=args.force)
    emit({"ok": True, "hint": "凭据已缓存，可直接使用 topics/follows/export"})


def cmd_topics(args):
    token, csrf = load_credentials()
    try:
        token, csrf = refresh_credentials_if_needed(token, csrf)
        topics = fetch_topics(token, csrf)
    except urllib.error.HTTPError as e:
        emit({"ok": False, "error": f"HTTP {e.code}", "hint": "403 则需重新 login"})
        return 2
    except Exception as e:
        emit({"ok": False, "error": explain_network_error(e)})
        return 2
    items = [{"alias": t.get("id_alias") or t.get("alias"), "name": t.get("name", "未命名知识库"),
              "follow_count": ((t.get("extend_data") or {}).get("stats") or {}).get("follow_count")
              or ((t.get("stats_info") or {}).get("follow_count"))}
             for t in topics]
    emit({"ok": True, "count": len(items), "items": items}, args.pretty)
    return 0


def cmd_follows(args):
    token, csrf = load_credentials()
    try:
        token, csrf = refresh_credentials_if_needed(token, csrf)
        follows = fetch_follows(args.topic_alias, token, csrf)
    except urllib.error.HTTPError as e:
        emit({"ok": False, "error": f"HTTP {e.code}"})
        return 2
    items = []
    for f in follows:
        fid = f.get("id") or f.get("follow_id")
        url = build_follow_url(args.topic_alias, fid, f.get("name", "未命名博主"))
        items.append({"name": f.get("name", "未命名博主"),
                      "platform": f.get("platform"),
                      "post_count": (f.get("extend_data") or {}).get("get_note_count") or f.get("post_count"),
                      "follow_id": fid,
                      "url": url})
    emit({"ok": True, "count": len(items), "items": items}, args.pretty)
    return 0


def cmd_export(args):
    token, csrf = load_credentials()
    try:
        token, csrf = refresh_credentials_if_needed(token, csrf)
    except RuntimeError as e:
        emit({"ok": False, "error": str(e)})
        return 2
    events = []

    def on_progress(idx, total, fname, status):
        if status == "ok":
            events.append(f"[{idx}/{total}] {fname}")
        elif isinstance(status, str) and status not in ("ok",):
            if status == "skip" and not args.verbose:
                return
            events.append(f"[{idx}/{total}] {status}: {fname}")

    if args.dry_run:
        sug = build_resume_suggestion(args.url, token, csrf, args.output, args.output_is_base)
        emit({"ok": True, "dry_run": True, **sug}, args.pretty)
        return 0

    try:
        summary = export(args.url, token, csrf, output_dir=args.output,
                         start_date=args.start_date, end_date=args.end_date,
                         output_is_base=args.output_is_base,
                         on_progress=on_progress if args.verbose else None,
                         max_items=args.max_items)
    except urllib.error.HTTPError as e:
        emit({"ok": False, "error": f"HTTP {e.code}",
              "hint": "Token 已过期，请重新 login" if e.code == 403 else None})
        return 2
    except ValueError as e:
        emit({"ok": False, "error": str(e)})
        return 2
    except Exception as e:
        emit({"ok": False, "error": explain_network_error(e)})
        return 2
    if args.verbose and events:
        summary["events"] = events[-50:]
    emit(summary, args.pretty)
    return 0


def cmd_status(args):
    token, csrf = load_credentials(require=False)
    sug = build_resume_suggestion(args.url, token, csrf, args.output, args.output_is_base)
    emit(sug, args.pretty)
    return 0 if sug.get("ok") else 2


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

    p = sub.add_parser("export", help="导出博主逐字稿")
    p.add_argument("url", help="博主订阅页 URL（用 follows 查到的 url）")
    p.add_argument("--output", "-o", help="输出目录（配合 --output-is-base 作为根目录）")
    p.add_argument("--output-is-base", action="store_true", help="把 --output 当根目录，自动建子目录")
    p.add_argument("--start-date", help="YYYY-MM-DD，缺省自动从上次断点续传")
    p.add_argument("--end-date", help="YYYY-MM-DD")
    p.add_argument("--max-items", type=int, help="单次最多导出篇数")
    p.add_argument("--dry-run", action="store_true", help="只看会导出什么，不落盘")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("status", help="查看导出状态与续传建议")
    p.add_argument("url")
    p.add_argument("--output", "-o")
    p.add_argument("--output-is-base", action="store_true")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
