#!/usr/bin/env python3
"""Sandbox loopback HTTP endpoint transported over sbx exec's stdin/stdout.

No outbound socket, host mount, shell command or arbitrary URL is accepted.
stdout is exclusively the JSONL transport; diagnostics go to stderr.
"""
import base64
import http.server
import json
import queue
import sys
import threading
import uuid

pending = {}
lock = threading.Lock()


def receive():
    try:
        for line in sys.stdin:
            packet = json.loads(line)
            with lock:
                target = pending.get(packet['id'])
            if target:
                target.put(packet)
    finally:
        with lock:
            for target in pending.values():
                target.put({'kind': 'end'})
        # Parent tunnel disappeared: don't leave a stale port occupied.
        import os
        os._exit(0)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.handle_request()

    def do_GET(self):
        self.handle_request()

    def handle_request(self):
        if self.path == '/health':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')
            return
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 <= size <= 32 * 1024 * 1024:
            self.send_error(413)
            return
        ident, target = uuid.uuid4().hex, queue.Queue()
        with lock:
            pending[ident] = target
            print(json.dumps({'id': ident, 'method': self.command, 'path': self.path,
                              'body': base64.b64encode(self.rfile.read(size)).decode()}), flush=True)
        try:
            packet = target.get(timeout=660)
            self.send_response(packet.get('status', 502))
            self.send_header('Content-Type', packet.get('type', 'application/json'))
            self.end_headers()
            while True:
                packet = target.get(timeout=660)
                if packet['kind'] == 'end':
                    break
                self.wfile.write(base64.b64decode(packet['data']))
                self.wfile.flush()
        except (OSError, queue.Empty):
            pass
        finally:
            with lock:
                pending.pop(ident, None)


if __name__ == '__main__':
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 2345), Handler)
    threading.Thread(target=receive, daemon=True).start()
    server.serve_forever()
