"""Call local Bot services using the match HTTP protocol."""
import http.client
import socket
import threading
import time

from .protocol import MAX_RESPONSE_BYTES, dumps, json_object, parse_reply


class HttpBot:
    """HTTP client for a locally launched Bot."""

    def __init__(self, port):
        self.port = port

    def exited(self):
        return False

    def request(self, method, path, body, limit):
        """Return transport status, HTTP status, body and elapsed microseconds."""
        if self.exited():
            return 'crash', 0, None, 0
        headers = {'Connection': 'close'}
        data = None
        if body is not None:
            data = dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        t0 = time.perf_counter()
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=limit)
        deadline = None
        try:
            conn.connect()
            sock = conn.sock
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            def cancel():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            remaining = limit - (time.perf_counter() - t0)
            if remaining <= 0:
                raise socket.timeout()
            sock.settimeout(remaining)
            # Bound the whole exchange, including slow headers and trickled bodies.
            deadline = threading.Timer(remaining, cancel)
            deadline.daemon = True
            deadline.start()
            conn.request(method, path, body=data, headers=headers)
            with conn.getresponse() as response:
                out = response.read(MAX_RESPONSE_BYTES + 1)
                if response.length and len(out) <= MAX_RESPONSE_BYTES:
                    raise http.client.IncompleteRead(out, response.length)
                code = response.status
        except (OSError, http.client.HTTPException, ValueError) as exc:
            us = int((time.perf_counter() - t0) * 1e6)
            status = 'timeout' if isinstance(exc, socket.timeout) or us >= limit * 1e6 else 'connection'
            return status, 0, str(exc)[:200], us
        finally:
            if deadline is not None:
                deadline.cancel()
            conn.close()
        us = int((time.perf_counter() - t0) * 1e6)
        if us > limit * 1e6:
            return 'timeout', code, None, us
        if len(out) > MAX_RESPONSE_BYTES:
            return 'invalid', code, None, us
        return 'ok', code, out, us

    # ---------------------------------------------------------------- 协议
    def start(self, limit_s):
        """等待就绪。返回 (ok, 说明)。"""
        end = time.monotonic() + limit_s
        while time.monotonic() < end:
            if self.exited():
                return False, 'crash'
            st, code, data, _ = self.request('GET', '/v1/health', None, min(1.0, max(0.001, end - time.monotonic())))
            if st == 'ok' and code == 200:
                try:
                    obj = json_object(data)
                except (ValueError, RecursionError):
                    obj = None
                if isinstance(obj, dict) and obj.get('ready') is True:
                    return True, 'ready'
            time.sleep(min(0.05, max(0, end - time.monotonic())))
        return False, 'startup' if not self.exited() else 'crash'

    def post(self, run_id, what, msg, limit_s):
        st, code, data, us = self.request('POST', f'/v1/matches/{run_id}/{what}', msg, limit_s)
        if st == 'ok' and not 200 <= code < 300:
            st = 'invalid'
        return st, code, data, us

    def turn(self, run_id, msg, limit_s):
        """返回命令、请求状态和耗时微秒。"""
        t = msg['turn']
        st, code, data, us = self.post(run_id, 'turns/%d' % t, msg, limit_s)
        if st != 'ok':
            return None, st, us
        r = parse_reply(data) if code == 200 else None
        if r is None or r[0] != t:
            return None, 'invalid', us
        return r[1], 'ok', us
