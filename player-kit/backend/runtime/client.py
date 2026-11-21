"""Call Bot services using the match HTTP protocol."""
import http.client
import os
import socket
import threading
import time
from urllib.parse import urlsplit

from game_core.protocol import MAX_RESPONSE_BYTES, VERSION, dumps, json_object, parse_reply


class HttpBot:
    """HTTP client for a Bot service whose lifecycle is managed by the caller."""

    kind = 'http'

    def __init__(self, url, *, token=None):
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('Bot URL 必须是 HTTP(S) 服务根地址')
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if self.scheme == 'https' else 80)
        self.token = token if token is not None else os.environ.get('BOT_TOKEN', '')
        self.info = {}

    def exited(self):
        return False

    def request(self, method, path, body, limit):
        """Return transport status, HTTP status, body and elapsed microseconds."""
        if self.exited():
            return 'crash', 0, None, 0
        headers = {'X-Arena-Protocol': VERSION, 'Connection': 'close'}
        if self.token:
            headers['X-Arena-Token'] = self.token
        data = None
        if body is not None:
            data = dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        t0 = time.perf_counter()
        connection = http.client.HTTPSConnection if self.scheme == 'https' else http.client.HTTPConnection
        conn = connection(self.host, self.port, timeout=limit)
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
                    self.info = obj
                    return True, 'ready'
            elif st == 'ok' and code in (401, 403):
                return False, 'startup'               # 令牌不对：不必再等
            time.sleep(min(0.05, max(0, end - time.monotonic())))
        return False, 'startup' if not self.exited() else 'crash'

    def post(self, mid, what, msg, limit_s):
        st, code, data, us = self.request('POST', '/v1/matches/%s/%s' % (mid, what), msg, limit_s)
        if st == 'ok' and not 200 <= code < 300:
            st = 'invalid'
        return st, code, data, us

    def turn(self, mid, msg, limit_s):
        """返回 (cmd, status, raw, us)，区分超时、连接失败、无效回复和请求前的程序退出。"""
        t = msg['turn']
        st, code, data, us = self.post(mid, 'turns/%d' % t, msg, limit_s)
        raw = data.decode('utf-8', 'replace')[:200] if isinstance(data, bytes) else data
        if st == 'crash':
            return None, 'crash', raw, us
        if st == 'timeout':
            return None, 'timeout', raw, us
        if st != 'ok' or code != 200:
            return None, 'connection' if st == 'connection' else 'invalid', raw, us
        r = parse_reply(data)
        if r is None or r[0] != t:
            return None, 'invalid', raw, us
        return r[1], 'ok', raw, us

    def close(self):
        pass
