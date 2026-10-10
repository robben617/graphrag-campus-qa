"""前端静态服务（禁用缓存）。

用于本地开发/演示时托管 frontend 目录。相比 python -m http.server，
本服务显式发送 Cache-Control: no-store，避免浏览器复用旧 JS/CSS 导致
"点击菜单空白"这类新旧文件不匹配的问题。

用法：
    python frontend_server.py                 # 默认 http://127.0.0.1:5500
    python frontend_server.py --port 8080
"""

from __future__ import annotations

import argparse
import functools
import http.server
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    """静态文件处理器：所有响应都禁用缓存。"""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args) -> None:  # noqa: A003
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="前端静态服务（禁用缓存）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5500)
    parser.add_argument("--dir", default=str(ROOT / "frontend"), help="静态目录")
    args = parser.parse_args(argv)

    handler = functools.partial(NoCacheHandler, directory=args.dir)
    httpd = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    print(f"前端服务已启动: http://{args.host}:{args.port}")
    print(f"静态目录: {args.dir}")
    print("已禁用浏览器缓存。按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
