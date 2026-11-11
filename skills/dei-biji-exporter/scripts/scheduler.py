"""每日自动同步：在本机系统里注册/查看/删除定时任务（不依赖任何 agent 常驻）。

  macOS   → ~/Library/LaunchAgents/com.dei-biji-exporter.sync.plist（launchd；睡眠错过的会在唤醒后补跑）
  Windows → 任务计划程序 “dei-biji-exporter-sync”（通过 .cmd 包装脚本调用，规避 /TR 长度限制）
  Linux   → 用户 crontab 里带标记的一行

定时任务执行的是：<当前 python> run.py sync，日志写到 <数据目录>/sync.log。
"""
import os
import platform
import plistlib
import subprocess
import sys
from pathlib import Path

import auth_helper

LABEL = "com.dei-biji-exporter.sync"
WIN_TASK = "dei-biji-exporter-sync"
CRON_MARK = "# dei-biji-exporter-sync"
HERE = Path(__file__).resolve().parent
LOG = auth_helper.DATA_DIR / "sync.log"


def _system():
    return os.environ.get("BIJI_FAKE_OS") or platform.system()  # 测试用：可伪装平台生成配置


def _cmd():
    return [sys.executable, str(HERE / "run.py"), "sync", "--quiet"]


def _env():
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    for k in ("BIJI_DATA_DIR", "BIJI_CONFIG", "BIJI_BROWSER", "BIJI_USE_SYSTEM_PROXY"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def plan(hour, minute):
    """返回 {system, path, content, commands}：要写哪个文件、内容是什么、要执行哪些系统命令。"""
    sysname = _system()
    if sysname == "Darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        content = plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": _cmd(),
            "WorkingDirectory": str(HERE),
            "StartCalendarInterval": {"Hour": hour, "Minute": minute},
            "EnvironmentVariables": _env(),
            "StandardOutPath": str(LOG),
            "StandardErrorPath": str(LOG),
        }).decode("utf-8")
        uid = os.getuid() if hasattr(os, "getuid") else 0
        cmds = [["launchctl", "bootout", f"gui/{uid}", str(path)],  # 已存在则先卸载（失败忽略）
                ["launchctl", "bootstrap", f"gui/{uid}", str(path)]]
        return {"system": "macOS", "path": str(path), "content": content, "commands": cmds}
    if sysname == "Windows":
        path = auth_helper.DATA_DIR / "biji_sync.cmd"
        sets = "".join(f'set "{k}={v}"\r\n' for k, v in _env().items())
        content = ("@echo off\r\nchcp 65001 >nul\r\n" + sets +
                   f'cd /d "{HERE}"\r\n'
                   f'"{sys.executable}" "{HERE / "run.py"}" sync --quiet >> "{LOG}" 2>&1\r\n')
        cmds = [["schtasks", "/Create", "/F", "/SC", "DAILY", "/TN", WIN_TASK,
                 "/ST", f"{hour:02d}:{minute:02d}", "/TR", f'"{path}"']]
        return {"system": "Windows", "path": str(path), "content": content, "commands": cmds}
    # Linux / 其他类 Unix
    envs = " ".join(f"{k}={v}" for k, v in _env().items())
    line = (f'{minute} {hour} * * * cd "{HERE}" && {envs} "{sys.executable}" run.py sync --quiet '
            f'>> "{LOG}" 2>&1 {CRON_MARK}')
    return {"system": sysname, "path": "crontab", "content": line, "commands": []}


def install(hour, minute, dry_run=False):
    p = plan(hour, minute)
    if dry_run:
        return {"ok": True, "dry_run": True, **p}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if p["path"] == "crontab":
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        lines = [l for l in cur.splitlines() if CRON_MARK not in l] + [p["content"]]
        r = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, capture_output=True)
        if r.returncode:
            return {"ok": False, "error": r.stderr.strip()}
    else:
        Path(p["path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(p["path"]).write_text(p["content"], encoding="utf-8")
        for i, c in enumerate(p["commands"]):
            r = subprocess.run(c, capture_output=True, text=True)
            if r.returncode and not (p["system"] == "macOS" and i == 0):  # bootout 失败=原本没装，正常
                return {"ok": False, "error": (r.stderr or r.stdout).strip(), "command": c}
    return {"ok": True, "system": p["system"], "time": f"{hour:02d}:{minute:02d}",
            "installed_at": p["path"], "log": str(LOG)}


def remove():
    sysname = _system()
    if sysname == "Darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        uid = os.getuid() if hasattr(os, "getuid") else 0
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
        existed = path.exists()
        if existed:
            path.unlink()
        return {"ok": True, "removed": existed}
    if sysname == "Windows":
        r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", WIN_TASK], capture_output=True, text=True)
        return {"ok": True, "removed": r.returncode == 0}
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    keep = [l for l in cur.splitlines() if CRON_MARK not in l]
    subprocess.run(["crontab", "-"], input="\n".join(keep) + "\n", text=True, capture_output=True)
    return {"ok": True, "removed": len(keep) != len(cur.splitlines())}


def status():
    sysname = _system()
    if sysname == "Darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        if not path.exists():
            return {"installed": False}
        cfg = plistlib.loads(path.read_bytes())
        t = cfg.get("StartCalendarInterval", {})
        uid = os.getuid() if hasattr(os, "getuid") else 0
        loaded = subprocess.run(["launchctl", "print", f"gui/{uid}/{LABEL}"], capture_output=True).returncode == 0
        return {"installed": True, "loaded": loaded, "time": f"{t.get('Hour', 0):02d}:{t.get('Minute', 0):02d}",
                "log": str(LOG)}
    if sysname == "Windows":
        r = subprocess.run(["schtasks", "/Query", "/TN", WIN_TASK, "/FO", "LIST"], capture_output=True, text=True)
        return {"installed": r.returncode == 0, "detail": r.stdout.strip()[:400], "log": str(LOG)}
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    line = next((l for l in cur.splitlines() if CRON_MARK in l), None)
    return {"installed": bool(line), "detail": line, "log": str(LOG)}


def notify(title, message):
    """尽力而为的系统通知（目前仅 macOS）；失败静默。"""
    if _system() == "Darwin":
        msg = message.replace('"', "'")
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
                       capture_output=True)
