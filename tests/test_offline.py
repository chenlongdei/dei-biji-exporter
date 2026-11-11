"""离线测试：不需要账号、不需要 playwright、不联网。

    python3 -m unittest discover -s tests -v

模拟学员环境：独立 BIJI_DATA_DIR（无 DouClaw、无历史登录态），
子进程里强制 PYTHONIOENCODING=gbk（= Windows 中文系统下被 agent 用管道捕获输出）。
"""
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "biji-exporter" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_server  # noqa: E402

URL = "https://www.biji.com/subject/TA/DEFAULT?followId=12&followName=%E8%B0%A2%E8%83%9C%E5%AD%90"


class Env:
    """一个干净的“学员电脑”：独立数据目录 + 导出目录 + 模拟 API。"""

    def __init__(self, token="OK", **mock_kw):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.srv, self.base, self.stats = mock_server.start(**mock_kw)
        self.data = self.dir / "data"
        self.out = self.dir / "out"
        self.config = self.dir / "config.json"
        self.config.write_text(json.dumps({"biji_export_base": str(self.out)}), encoding="utf-8")
        if token:
            self.data.mkdir()
            (self.data / "skill_credentials.json").write_text(json.dumps({"token": token, "csrf": "c"}))

    def env(self, gbk=True):
        e = dict(os.environ, BIJI_DATA_DIR=str(self.data), BIJI_API_BASE=self.base, BIJI_CONFIG=str(self.config))
        e.pop("PYTHONUTF8", None)
        if gbk:
            e["PYTHONIOENCODING"] = "gbk"
        return e

    def run(self, *args, gbk=True):
        r = subprocess.run([sys.executable, *map(str, args)], cwd=SCRIPTS, env=self.env(gbk),
                           capture_output=True)
        out = r.stdout.decode("utf-8", errors="strict")  # 必须是合法 utf-8，不能是 gbk 乱码
        err = r.stderr.decode("utf-8", errors="replace")
        js = None
        for line in [out.strip()] + out.strip().splitlines()[::-1]:
            try:
                js = json.loads(line)
                break
            except Exception:
                continue
        return r.returncode, js, out, err

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()


class ExportTests(unittest.TestCase):
    def test_export_resume_and_gbk_console(self):
        e = Env(n_posts=60)
        try:
            code, js, out, err = e.run("biji_export.py", "export", URL, "-o", e.out, "--max-items", "10")
            self.assertEqual(code, 0, err)
            self.assertEqual(js["exported"], 10)
            self.assertFalse(js["complete"])
            self.assertNotIn("Traceback", err)
            code, js, _, err = e.run("biji_export.py", "export", URL, "-o", e.out)
            self.assertEqual(code, 0, err)
            self.assertTrue(js["complete"], js)
            self.assertEqual(js["skipped_existing"], 10)
            mds = list(e.out.glob("*.md"))
            self.assertEqual(len([m for m in mds if not m.name.startswith("_")]), 60 - js["skipped_empty"])
            self.assertTrue((e.out / "_空稿清单.md").exists())
            self.assertTrue((e.out / "links.json").exists())
            # 多行标题 → 单行标题 + 简介；康熙部首 → 正常汉字
            multi = next(e.out.glob("*_换行.md")).read_text(encoding="utf-8").split("\n\n")
            self.assertTrue(multi[0].startswith("# 换行") and multi[1].startswith("> 简介：标题"), multi[:2])
            self.assertTrue(list(e.out.glob("*_打工人的自我*.md")))
            # 编号连续且唯一
            idx = sorted(int(m.name.split("_")[0]) for m in mds if m.name[0].isdigit())
            self.assertEqual(idx, list(range(1, len(idx) + 1)))
        finally:
            e.close()

    def test_concurrency_is_used(self):
        e = Env(n_posts=24, latency=0.2)
        try:
            code, js, _, err = e.run("biji_export.py", "export", URL, "-o", e.out, "--workers", "4")
            self.assertEqual(code, 0, err)
            self.assertGreaterEqual(e.stats["max_conc"], 3)
            self.assertLess(js["seconds"], 24 * 0.2 * 0.6)  # 明显快于串行
        finally:
            e.close()

    def test_5xx_are_retried(self):
        e = Env(n_posts=40, fail_every=7)
        try:
            code, js, _, err = e.run("biji_export.py", "export", URL, "-o", e.out)
            self.assertEqual(code, 0, err)
            self.assertEqual(js["failed"], 0, js)
            self.assertTrue(js["complete"])
        finally:
            e.close()

    def test_token_expiry_midway_refreshes(self):
        import auth_helper
        import biji_export as be
        e = Env(n_posts=50, f403_after=15)
        orig_base, orig_refresh = be.API_BASE, auth_helper.headless_refresh
        be.API_BASE = e.base
        calls = []
        auth_helper.headless_refresh = lambda: (calls.append(1), ("FRESH", "c", None))[1]
        try:
            s = be.export(URL, "OK", "c", output_dir=str(e.out), workers=3)
            self.assertTrue(s["ok"] and s["complete"], s)
            self.assertEqual(len(calls), 1)  # 多线程同时 403 也只刷新一次
        finally:
            be.API_BASE, auth_helper.headless_refresh = orig_base, orig_refresh
            e.close()

    def test_token_expiry_refresh_fails_keeps_files(self):
        import auth_helper
        import biji_export as be
        e = Env(n_posts=50, f403_after=15)
        orig_base, orig_refresh = be.API_BASE, auth_helper.headless_refresh
        be.API_BASE = e.base
        auth_helper.headless_refresh = lambda: (None, None, "模拟失败")
        try:
            s = be.export(URL, "OK", "c", output_dir=str(e.out), workers=3)
            self.assertFalse(s["ok"])
            self.assertEqual(s["error_code"], "token_expired")
            self.assertGreater(s["exported"], 5)
            self.assertTrue((e.out / "links.json").exists())
        finally:
            be.API_BASE, auth_helper.headless_refresh = orig_base, orig_refresh
            e.close()

    def test_transient_empty_list_is_retried(self):
        e = Env(n_posts=30, empty_list_calls=2)
        try:
            code, js, _, err = e.run("biji_export.py", "export", URL, "-o", e.out)
            self.assertEqual(code, 0, err)
            self.assertTrue(js["complete"], js)
            self.assertEqual(js["total_found"], 30)
        finally:
            e.close()

    def test_persistent_empty_list_is_not_complete(self):
        e = Env(n_posts=30, empty_list_calls=99)
        try:
            code, js, _, err = e.run("biji_export.py", "export", URL, "-o", e.out)
            self.assertEqual(code, 2, err)
            self.assertEqual(js["error_code"], "list_incomplete")  # 绝不能报 complete
        finally:
            e.close()

    def test_not_logged_in_is_explicit(self):
        e = Env(token=None)
        try:
            code, js, _, err = e.run("run.py", "export-biji", "谢胜子")
            self.assertEqual(code, 2, err)
            self.assertEqual(js["error_code"], "not_logged_in")
            self.assertIn("login", js["next_action"])
        finally:
            e.close()

    def test_name_matching(self):
        e = Env()
        try:
            code, js, _, _ = e.run("run.py", "export-biji", "王")
            self.assertEqual(code, 3)
            self.assertEqual(js["error_code"], "ambiguous")
            self.assertEqual(len(js["candidates"]), 3)  # 王小明 / 王小明二号 / 老王
            code, js, _, _ = e.run("biji_export.py", "find", "王小明")  # 完全同名优先于“王小明二号”
            self.assertEqual(code, 0)
            self.assertEqual(js["match"]["follow_id"], 11)
            code, js, _, _ = e.run("run.py", "export-biji", "不存在的人")
            self.assertEqual(js["error_code"], "not_found")
        finally:
            e.close()

    def test_run_py_end_to_end_gbk(self):
        e = Env(n_posts=8)
        try:
            code, js, out, err = e.run("run.py", "export-biji", "谢胜子", "-o", e.out)
            self.assertEqual(code, 0, err)
            self.assertTrue(js["complete"], js)
            self.assertNotIn("Traceback", err)
        finally:
            e.close()

    def test_doctor_runs_without_login(self):
        e = Env(token=None)
        try:
            code, js, _, err = e.run("biji_export.py", "doctor", "--skip-browser")
            self.assertIsNotNone(js, err)
            names = {c["name"]: c for c in js["checks"]}
            self.assertFalse(names["login"]["ok"])
            self.assertTrue(js["next_action"])
        finally:
            e.close()


class WorkflowTests(unittest.TestCase):
    """学员动线：list → 选人/时间段 → 导出 → 加入每日同步 → 定时任务。"""

    def test_list_shows_topics_and_bloggers(self):
        e = Env()
        try:
            code, js, _, err = e.run("run.py", "list")
            self.assertEqual(code, 0, err)
            self.assertEqual(js["topic_count"], 2)
            self.assertEqual(js["blogger_count"], 4)
            self.assertIn("谢胜子", err)  # 易读表格在 stderr
        finally:
            e.close()

    def test_date_range(self):
        e = Env(n_posts=60)
        try:
            code, js, _, err = e.run("run.py", "export-biji", "谢胜子", "--start-date", "2025-03-01",
                                     "--end-date", "2025-04-30")
            self.assertEqual(code, 0, err)
            for m in Path(js["output_dir"]).glob("*.md"):
                if m.name[0].isdigit():
                    head = m.read_text(encoding="utf-8")[:600]
                    self.assertRegex(head, r"发布时间：2025-0[34]-")
            self.assertLess(js["total_found"], 60)
        finally:
            e.close()

    def test_watch_sync_and_new_videos(self):
        e = Env(n_posts=20)
        try:
            code, js, _, _ = e.run("run.py", "sync")
            self.assertEqual(js["error_code"], "empty_watch")
            code, js, _, err = e.run("run.py", "watch", "add", "谢胜子")
            self.assertEqual(code, 0, err)
            code, js, _, _ = e.run("run.py", "watch", "add", "谢胜子")  # 重复添加不重复
            self.assertEqual(len(js["watch"]), 1)
            code, js, _, err = e.run("run.py", "sync", "--quiet")
            self.assertEqual(code, 0, err)
            first = js["new_total"]
            self.assertGreater(first, 15)
            e.stats["n_posts"] = 25                      # 第二天博主发了 5 条新视频
            code, js, _, err = e.run("run.py", "sync", "--quiet")
            self.assertEqual(code, 0, err)
            self.assertEqual(js["new_total"], 4)          # 5 条里 i=22 在模拟数据中是空稿
            self.assertTrue((e.out / "_同步日志.md").exists())
            code, js, _, _ = e.run("run.py", "watch", "remove", "谢胜子")
            self.assertEqual(js["removed"], 1)
        finally:
            e.close()

    def test_sync_reports_expired_login(self):
        e = Env(token="EXPIRED")
        try:
            e.config.write_text(json.dumps({"biji_export_base": str(e.out),
                                            "watch": [{"name": "谢胜子", "url": URL}]}), encoding="utf-8")
            env_noplaywright = e.env()
            env_noplaywright["BIJI_BROWSER"] = "no-such-browser"  # 无头刷新必然失败
            r = subprocess.run([sys.executable, "run.py", "sync", "--quiet"], cwd=SCRIPTS,
                               env=env_noplaywright, capture_output=True)
            js = json.loads(r.stdout.decode("utf-8"))
            self.assertEqual(r.returncode, 2)
            self.assertEqual(js["error_code"], "token_expired")
        finally:
            e.close()

    def test_schedule_dry_run_all_platforms(self):
        e = Env()
        try:
            e.config.write_text(json.dumps({"watch": [{"name": "x", "url": URL}]}), encoding="utf-8")
            for fake, expect in (("Darwin", "StartCalendarInterval"), ("Windows", "schtasks"),
                                 ("Linux", "# biji-exporter-sync")):
                env = e.env()
                env["BIJI_FAKE_OS"] = fake
                r = subprocess.run([sys.executable, "run.py", "schedule", "install", "--time", "09:30",
                                    "--dry-run"], cwd=SCRIPTS, env=env, capture_output=True)
                js = json.loads(r.stdout.decode("utf-8"))
                self.assertTrue(js["ok"] and js["dry_run"], js)
                blob = js["content"] + json.dumps(js["commands"])
                self.assertIn(expect, blob, fake)
                self.assertIn("sync", blob)
            r = subprocess.run([sys.executable, "run.py", "schedule", "install", "--time", "25:00",
                                "--dry-run"], cwd=SCRIPTS, env=e.env(), capture_output=True)
            self.assertEqual(json.loads(r.stdout.decode("utf-8"))["error_code"], "bad_input")
        finally:
            e.close()


def _write_csv(path, rows, enc="utf-8-sig", delim=","):
    buf = io.StringIO()
    csv.writer(buf, delimiter=delim).writerows(rows)
    data = buf.getvalue()
    path.write_bytes(data.encode(enc) if enc != "utf-16" else data.encode("utf-16"))


def _write_xlsx(path, rows):
    shared, sheet_rows = [], []
    for ri, row in enumerate(rows, 1):
        cells = []
        for ci, v in enumerate(row):
            ref = chr(65 + ci) + str(ri)
            if isinstance(v, (int, float)):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            else:
                shared.append(v)
                cells.append(f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>')
        sheet_rows.append(f'<row r="{ri}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    from xml.sax.saxutils import escape
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rns}><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(f"<si><t>{escape(s)}</t></si>" for s in shared) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData>{"".join(sheet_rows)}</sheetData></worksheet>')


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def merge(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="gbk")
        r = subprocess.run([sys.executable, "merge_tables.py", *map(str, args)], cwd=SCRIPTS,
                           env=env, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        return json.loads(r.stdout.decode("utf-8"))

    def read_out(self, p):
        return list(csv.DictReader(io.StringIO(Path(p).read_text(encoding="utf-8-sig"), newline="")))

    def test_encodings_xlsx_and_keep_last(self):
        d = self.d / "in"
        d.mkdir()
        _write_csv(d / "1月.csv", [["视频id", "标题", "点赞"], ["7301", "A", "10"], ["7302", "B", "20"]])
        _write_csv(d / "2月.csv", [["视频id", "标题", "点赞"], ["7302", "B", "25"], ["7303", "瓩囍C", "30"]], enc="gbk")
        (d / "3月.csv").write_bytes("视频id,标题,点赞\n7304,扩展字㐀D,40\n".encode("gb18030"))
        _write_csv(d / "4月.csv", [["视频id", "标题", "点赞"], ["7305", "E", "50"]], enc="utf-16", delim="\t")
        _write_xlsx(d / "5月.xlsx", [["视频id", "标题", "点赞"], [7306, "F", 60], [7301.0, "", 11]])
        (d / "~$5月.xlsx").write_bytes(b"lock")
        js = self.merge(d)
        self.assertEqual(js["skipped"], [], js)
        self.assertEqual(js["files"], 5)
        rows = {r["视频id"]: r for r in self.read_out(js["output_csv"])}
        self.assertEqual(sorted(rows), ["7301", "7302", "7303", "7304", "7305", "7306"])
        self.assertEqual(rows["7302"]["点赞"], "25")      # 新数据覆盖旧数据
        self.assertEqual(rows["7301"]["点赞"], "11")
        self.assertEqual(rows["7301"]["标题"], "A")       # 新表空格子沿用旧值
        self.assertEqual(rows["7304"]["标题"], "扩展字㐀D")
        js2 = self.merge(d)                               # 再合并一次：不能把上次的 merged_*.csv 读进来
        self.assertEqual(js2["files"], 5)

    def test_join_normalizes_links_and_multiline(self):
        _write_csv(self.d / "a.csv", [["视频链接", "标题", "反馈"],
                                      ["https://v.douyin.com/a?x=1", "A", "第一行\n第二行"],
                                      ["https://v.douyin.com/b/", "B", "ok"]])
        _write_csv(self.d / "b.csv", [["视频链接", "评论数"], ["https://v.douyin.com/a", "3"],
                                      ["https://V.douyin.com/b", "5"]])
        js = self.merge(self.d / "a.csv", self.d / "b.csv", "-o", self.d / "o.csv")
        self.assertEqual(js["mode"], "join")
        self.assertEqual(js["join_stats"][0]["matched"], 2)
        rows = self.read_out(self.d / "o.csv")
        self.assertEqual(rows[0]["反馈"], "第一行\n第二行")

    def test_json_with_bom(self):
        (self.d / "x.json").write_text(json.dumps({"items": [{"id": "1", "v": "中文"}]}, ensure_ascii=False),
                                       encoding="utf-8-sig")
        js = self.merge(self.d / "x.json", "-o", self.d / "o.csv")
        self.assertEqual(js["rows_out"], 1)


if __name__ == "__main__":
    unittest.main()
