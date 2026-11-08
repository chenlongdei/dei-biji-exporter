"""
biji-export skill - 授权模块
通过 Playwright + 持久化用户数据完成授权，保存登录状态避免重复登录。
还原自 DouClaw v1.0.1（用户自有软件），登录态路径与原 app 隔离。
"""
import json
import time
from pathlib import Path

USER_DATA_DIR = Path.home() / ".biji_exporter" / "browser_data_skill"
CRED_CACHE = Path.home() / ".biji_exporter" / "skill_credentials.json"
BIJI_HOME = "https://www.biji.com"
LOGIN_TIMEOUT = 300  # 5 分钟


def _read_token(page):
    return page.evaluate("() => localStorage.getItem('token') || ''")


def _read_csrf(page):
    return page.evaluate(
        "() => {\n"
        "        const cookie = document.cookie;\n"
        "        const match = cookie.match(/csrfToken=([^;]+)/);\n"
        "        return match ? match[1] : '';\n"
        "    }"
    )


def save_credentials(token: str, csrf: str) -> None:
    """把 token/csrf 缓存到磁盘，供 CLI 免登录复用。"""
    CRED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CRED_CACHE.write_text(
        json.dumps({"token": token, "csrf": csrf, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
        encoding="utf-8",
    )


def load_cached_credentials():
    """返回 (token, csrf) 或 (None, None)。"""
    try:
        data = json.loads(CRED_CACHE.read_text(encoding="utf-8"))
        return data.get("token"), data.get("csrf")
    except Exception:
        return None, None


def extract_token_interactive(force_reauth: bool = False):
    """
    使用 Playwright 启动持久化浏览器，保存登录状态。
    force_reauth=True 时，要求用户在浏览器里退出并切换账号，
    只有检测到 token 变化后才会返回。
    返回 (token, csrf)。
    """
    from playwright.sync_api import sync_playwright, Error as PWError

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        print("🌐 正在启动浏览器...")
        launch_args = ["--start-maximized"]
        if not _use_system_proxy():
            launch_args.append("--no-proxy-server")
        _ensure_browsers_path()
        context = p.chromium.launch_persistent_context(
            str(USER_DATA_DIR),
            headless=False,
            args=launch_args,
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(BIJI_HOME, wait_until="networkidle")

        token = _read_token(page)
        csrf = _read_csrf(page)

        if token and not force_reauth:
            print("✅ 检测到已登录状态，无需重新登录")
            context.close()
            save_credentials(token, csrf)
            return token, csrf

        old_token = token
        if force_reauth and token:
            print("💡 已进入重新登录模式，请先退出当前账号，再登录目标账号。")
            print("   检测到新账号登录成功后，此窗口会自动关闭。")
        else:
            print("💡 首次使用需要登录，登录后会保存状态，下次无需重复登录。")

        waited = 0
        interval = 2
        while waited < LOGIN_TIMEOUT:
            time.sleep(interval)
            waited += interval
            try:
                token = _read_token(page)
            except PWError:
                print("登录窗口已被关闭，请重新发起登录")
                raise RuntimeError("登录窗口已被关闭")
            if token:
                if force_reauth and token == old_token:
                    # 还是旧账号，继续等切换
                    print(f"   等待切换账号中... ({waited}s / {LOGIN_TIMEOUT}s)")
                    continue
                print("✅ 检测到登录成功！" if not force_reauth else "✅ 检测到新的登录账号，已完成切换")
                csrf = _read_csrf(page)
                break
            print(f"   等待登录中... ({waited}s / {LOGIN_TIMEOUT}s)")
        else:
            msg = "等待切换账号超时（5分钟），请重试" if force_reauth else "等待登录超时（5分钟），请重试"
            try:
                context.close()
            except Exception:
                pass
            raise RuntimeError(msg)

        context.close()
        save_credentials(token, csrf)
        print("✅ 授权信息提取成功")
        return token, csrf


def _ensure_browsers_path():
    """指向 DouClaw 的 chromium，避免重新下载浏览器。"""
    import os
    from pathlib import Path
    if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(
            Path.home() / "Library/Application Support/DouClaw/playwright-browsers")


def _use_system_proxy() -> bool:
    import os
    return os.environ.get("BIJI_USE_SYSTEM_PROXY", "").lower() in ("1", "true", "yes")


if __name__ == "__main__":
    import sys
    extract_token_interactive(force_reauth="--force" in sys.argv)
