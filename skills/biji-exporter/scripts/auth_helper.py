"""
biji-export skill - 授权模块
通过 Playwright + 持久化用户数据完成授权，保存登录状态避免重复登录。
还原自 DouClaw v1.0.1（用户自有软件），登录态路径与原 app 隔离。

浏览器选择（不依赖任何特定软件的安装路径）：
  系统 Google Chrome → Microsoft Edge（Windows 自带）→ playwright 自带 chromium
  环境变量 BIJI_BROWSER=chrome|msedge|chromium 可强制指定。
数据目录：默认 ~/.biji_exporter，环境变量 BIJI_DATA_DIR 可覆盖（测试/多账号隔离用）。
"""
import json
import os
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):  # Windows 中文系统 + agent 管道捕获时默认 GBK，emoji 会崩
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DATA_DIR = Path(os.environ.get("BIJI_DATA_DIR") or (Path.home() / ".biji_exporter")).expanduser()
USER_DATA_DIR = DATA_DIR / "browser_data_skill"
CRED_CACHE = DATA_DIR / "skill_credentials.json"
BIJI_HOME = "https://www.biji.com"
LOGIN_TIMEOUT = 300  # 5 分钟
BROWSER_CHANNELS = ("chrome", "msedge", None)  # None = playwright 自带 chromium

INSTALL_HINT = ("找不到可用的浏览器。任选其一：安装 Google Chrome（推荐）；"
                "或运行 python3 -m playwright install chromium（Windows 用 python）")


def use_system_proxy() -> bool:
    return os.environ.get("BIJI_USE_SYSTEM_PROXY", "").lower() in ("1", "true", "yes")


def browser_order():
    forced = os.environ.get("BIJI_BROWSER", "").strip().lower()
    if forced:
        return [None if forced in ("chromium", "bundled") else forced]
    return list(BROWSER_CHANNELS)


def launch_context(p, headless):
    """依次尝试 Chrome → Edge → 自带 chromium，返回 (context, 浏览器名)。都不行抛 RuntimeError。"""
    args = [] if use_system_proxy() else ["--no-proxy-server"]
    if not headless:
        args.append("--start-maximized")
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    errors = []
    for ch in browser_order():
        kw = {"headless": headless, "args": args}
        if not headless:
            kw["no_viewport"] = True
        if ch:
            kw["channel"] = ch
        try:
            return p.chromium.launch_persistent_context(str(USER_DATA_DIR), **kw), (ch or "chromium")
        except Exception as e:
            errors.append(f"{ch or 'chromium'}: {str(e).strip().splitlines()[0][:160]}")
    raise RuntimeError(INSTALL_HINT + "\n" + "\n".join(errors))


def _read_token(page):
    return page.evaluate("() => localStorage.getItem('token') || ''")


def _read_csrf(page):
    return page.evaluate(
        "() => { const m = document.cookie.match(/csrfToken=([^;]+)/); return m ? m[1] : ''; }")


def save_credentials(token: str, csrf: str) -> None:
    """把 token/csrf 缓存到磁盘，供 CLI 免登录复用。"""
    CRED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CRED_CACHE.write_text(
        json.dumps({"token": token, "csrf": csrf, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def load_cached_credentials():
    """返回 (token, csrf) 或 (None, None)。"""
    try:
        data = json.loads(CRED_CACHE.read_text(encoding="utf-8"))
        return data.get("token"), data.get("csrf")
    except Exception:
        return None, None


CLOSE_WATCHDOG_SEC = 15


def _finish_login(context, token, csrf, msg):
    """先存凭据再关浏览器：真 Chrome 下 context.close() 偶发挂死（实测 macOS + Chrome 157），
    存好后挂死也不丢登录；看门狗到点直接报告成功并退出，不让 agent 干等。"""
    save_credentials(token, csrf)
    print(msg, flush=True)

    def _bail():
        print(json.dumps({"ok": True, "hint": "凭据已缓存（浏览器关闭超时，已强制结束）"},
                         ensure_ascii=False), flush=True)
        os._exit(0)

    import threading
    wd = threading.Timer(CLOSE_WATCHDOG_SEC, _bail)
    wd.daemon = True
    wd.start()
    try:
        context.close()
    except Exception:
        pass
    return wd


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        raise RuntimeError("没装 playwright：运行 python3 -m pip install --user playwright"
                           "（国内可加 -i https://pypi.tuna.tsinghua.edu.cn/simple）")


def headless_refresh():
    """用已保存的登录态无头打开 biji.com 读取新 token。返回 (token, csrf, error)。"""
    try:
        sync_playwright = _import_playwright()
        with sync_playwright() as p:
            ctx, _ = launch_context(p, headless=True)
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(BIJI_HOME, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)
                token, csrf = _read_token(page), _read_csrf(page)
            finally:
                ctx.close()
        if not token:
            return None, None, "登录态已失效（浏览器里也没有 token），需要重新 login"
        save_credentials(token, csrf)
        return token, csrf or "", None
    except Exception as e:
        return None, None, str(e)[:300]


def extract_token_interactive(force_reauth: bool = False):
    """
    启动持久化浏览器，等用户登录后保存凭据。
    force_reauth=True 时要求用户退出并切换账号，检测到 token 变化才返回。
    返回 (token, csrf)。
    """
    sync_playwright = _import_playwright()
    from playwright.sync_api import Error as PWError

    with sync_playwright() as p:
        print("🌐 正在启动浏览器...", flush=True)
        context, name = launch_context(p, headless=False)
        print(f"   使用浏览器：{name}", flush=True)
        page = context.pages[0] if context.pages else context.new_page()
        # networkidle 在有长连接的页面上可能永远等不到，改用 domcontentloaded
        page.goto(BIJI_HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        token = _read_token(page)
        csrf = _read_csrf(page)

        if token and not force_reauth:
            _finish_login(context, token, csrf, "✅ 检测到已登录状态，无需重新登录")
            return token, csrf

        old_token = token
        if force_reauth and token:
            print("💡 已进入重新登录模式，请先退出当前账号，再登录目标账号。", flush=True)
            print("   检测到新账号登录成功后，此窗口会自动关闭。", flush=True)
        else:
            print("💡 请在弹出的浏览器里登录（扫码即可），登录后会保存状态，下次无需重复登录。", flush=True)

        waited, interval = 0, 2
        while waited < LOGIN_TIMEOUT:
            time.sleep(interval)
            waited += interval
            try:
                token = _read_token(page)
            except PWError:
                raise RuntimeError("登录窗口已被关闭，请重新发起登录")
            if token:
                if force_reauth and token == old_token:
                    if waited % 10 == 0:
                        print(f"   等待切换账号中... ({waited}s / {LOGIN_TIMEOUT}s)", flush=True)
                    continue
                print("✅ 检测到登录成功！" if not force_reauth else "✅ 检测到新的登录账号，已完成切换", flush=True)
                save_credentials(token, csrf)  # 先落一版，后面任何一步出事都不丢登录
                try:
                    page.wait_for_timeout(1500)  # 等 csrf cookie 落地
                    csrf = _read_csrf(page) or csrf
                except Exception:
                    pass
                break
            if waited % 10 == 0:
                print(f"   等待登录中... ({waited}s / {LOGIN_TIMEOUT}s)", flush=True)
        else:
            try:
                context.close()
            except Exception:
                pass
            raise RuntimeError("等待切换账号超时（5分钟），请重试" if force_reauth else "等待登录超时（5分钟），请重试")

        _finish_login(context, token, csrf, "✅ 授权信息提取成功")
        return token, csrf


if __name__ == "__main__":
    extract_token_interactive(force_reauth="--force" in sys.argv)
