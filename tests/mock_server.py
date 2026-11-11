"""模拟 Get笔记 API（knowledge-api.trytalks.com）供离线测试。

start(n_posts=..., latency=..., f403_after=..., fail_every=...) -> (server, base_url, stats)
- f403_after：第 N 次 detail 之后，旧 token 一律 403（模拟 30 分钟过期）；token=FRESH 不受影响
- fail_every：每第 N 次 detail 返回 500（模拟服务端抖动）
"""
import json
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

TITLES = ["普通标题{}", "带emoji🔥标题{}", "斜杠/冒号:问号?{}", "超长标题" + "很长" * 60 + "{}",
          "CON", "  ..点开头{}", "换行\n标题{}", "打⼯⼈的⾃我{}"]
FOLLOWS = {"TA": [{"id": 11, "name": "王小明"}, {"id": 12, "name": "谢胜子"}],
           "TB": [{"id": 21, "name": "王小明二号"}, {"id": 22, "name": "老王"}]}


def _post(i):
    return {"post_id_str": str(9000000 + i), "post_name": TITLES[i % len(TITLES)].format(i),
            "post_publish_time": f"2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d} 10:00:00"}


def start(n_posts=120, latency=0.0, f403_after=0, fail_every=0, empty_list_calls=0):
    """empty_list_calls：前 N 次列表请求返回空页（真机观察到的限流表现：h.c=0 但 posts 为空）。"""
    stats = {"detail": 0, "max_conc": 0, "conc": 0, "list": 0, "n_posts": n_posts}
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            b = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _expired(self):
            return self.headers.get("Authorization") == "Bearer EXPIRED"

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if self._expired():
                return self._send(403, {})
            if u.path.endswith("/subscribe/topic/list"):
                return self._send(200, {"h": {}, "c": {"list": [{"id_alias": "TA", "name": "库A"},
                                                                 {"id_alias": "TB", "name": "库B"}],
                                                        "has_more": False}})
            if u.path.endswith("/follow/list"):
                return self._send(200, {"c": {"list": FOLLOWS.get(q["topic_id_alias"][0], []),
                                              "has_next": False}})
            if u.path.endswith("/topic/detail"):
                return self._send(200, {"c": {"topic_id": 777}})
            self._send(404, {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            if self.path.endswith("/follow/account/posts"):
                p, ps = body["page"], body["page_size"]
                with lock:
                    stats["list"] += 1
                    n = stats["list"]
                if n <= empty_list_calls:
                    return self._send(200, {"h": {"c": 0}, "c": {"posts": [], "has_more": False,
                                                                  "total": stats["n_posts"]}})
                N = stats["n_posts"]
                items = [_post(i) for i in range((p - 1) * ps, min(p * ps, N))]
                return self._send(200, {"h": {"c": 0}, "c": {"posts": items, "has_more": p * ps < N, "total": N}})
            if self.path.endswith("/topic/post/detail"):
                with lock:
                    stats["detail"] += 1
                    n = stats["detail"]
                    stats["conc"] += 1
                    stats["max_conc"] = max(stats["max_conc"], stats["conc"])
                try:
                    time.sleep(latency)
                    if f403_after and n > f403_after and self.headers.get("Authorization") != "Bearer FRESH":
                        return self._send(403, {})
                    if fail_every and n % fail_every == 0:
                        return self._send(500, {})
                    i = int(body["post_id"]) - 9000000
                    txt = "" if i % 17 == 5 else f"第{i}篇逐字稿。\n含emoji😀和特殊字符 ✅。\n" * 20
                    return self._send(200, {"c": {**_post(i), "post_media_text": txt,
                                                  "post_url": f"https://www.douyin.com/video/{i}?share=1"}})
                finally:
                    with lock:
                        stats["conc"] -= 1
            self._send(404, {})

    class S(ThreadingHTTPServer):
        daemon_threads = True

    srv = S(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", stats
