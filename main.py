#!/usr/bin/env python3
"""KickLur — BF Kicker bot. Entry point.

Long-polls Telegram. If PORT is set (Railway sets it for web services) a tiny
health endpoint is served so the platform can see the service is alive; it is
not needed for the bot to work.
"""
import os
import signal
import sys
import threading
import time

from kicklur import config as cfg
from kicklur.bot import KickLurBot
from kicklur.telegram import Telegram


def _start_health_server(port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"ok":true,"service":"kicklur"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

        def log_message(self, *args):
            pass

    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print(f"[KICKLUR] health endpoint on 0.0.0.0:{port}", flush=True)
    except Exception as e:
        print(f"[KICKLUR] health endpoint FAILED: {e}", flush=True)


def main():
    print("=" * 58)
    print("  KICKLUR — BF KICKER")
    print("=" * 58)

    problems = cfg.validate()
    if problems:
        for p in problems:
            print(f"[FATAL] {p}")
        sys.exit(1)

    print(f"  owner        : {cfg.OWNER_CHAT_ID}")
    print(f"  workers/run  : {cfg.BF_WORKERS}")
    print(f"  max global   : {cfg.BF_MAX_CONCURRENCY}")
    print(f"  device cap   : {cfg.BF_MAX_DEVICES}")
    print(f"  kick delay   : {cfg.BF_KICK_DELAY}s")
    print(f"  login server : {cfg.KICK_HOST}:{cfg.KICK_PORT}")
    print(f"  cli version  : {cfg.KICK_CLI_VERSION}")
    print(f"  channel      : {cfg.KICK_CHANNEL}")
    print(f"  lookup       : {cfg.BF_LOOKUP}")
    print(f"  ack wait     : {cfg.BF_ACK_WAIT}s")
    print(f"  fan-out      : {cfg.BF_KICK_ALL_SERVERS}")
    print(f"  kick twice   : {cfg.BF_KICK_TWICE}")
    print("=" * 58)

    tg = Telegram(cfg.BOT_TOKEN)
    me = tg.me()
    if not me.get("ok"):
        print("[FATAL] token tidak valid / Telegram tidak bisa dihubungi")
        sys.exit(1)
    print(f"[KICKLUR] bot @{me['result'].get('username')}", flush=True)

    if cfg.PORT:
        try:
            _start_health_server(cfg.PORT)
        except Exception as e:
            print(f"[KICKLUR] health endpoint skipped: {e}", flush=True)

    bot = KickLurBot(tg)

    def _stop(signum, frame):
        print(f"\n[KICKLUR] signal {signum}, keluar...", flush=True)
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except Exception:
            pass

    while True:
        try:
            bot.poll_forever()
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[KICKLUR] fatal: {type(e).__name__}: {e}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()
