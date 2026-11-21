"""Arena HTTP transport and run status contract."""
from http.client import HTTPConnection, HTTPException
from urllib.parse import quote, urlsplit

from engine.protocol import dumps, json_object

from .domain import PLATFORM_ERRORS


ACTIVE_ARENA_STATES = ('queued', 'running')
RUN_QUERY_BATCH_SIZE = 100


def parse_peer_url(base_url):
    if not isinstance(base_url, str) or any(char.isspace() for char in base_url):
        raise ValueError('内部服务地址必须为 HTTP 基础 URL')
    url = urlsplit(base_url)
    if (url.scheme != 'http' or not url.hostname or url.username is not None
            or url.query or url.fragment or (url.port is not None and url.port == 0)):
        raise ValueError('内部服务地址必须为 HTTP 基础 URL')
    return url


class ArenaClient:
    def __init__(self, base_url):
        url = parse_peer_url(base_url)
        self.host, self.port, self.prefix = url.hostname, url.port, url.path.rstrip('/')

    def _request(self, method, path, body=None):
        client = HTTPConnection(self.host, self.port, timeout=5)
        headers = {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            body = dumps(body).encode('utf-8')
        try:
            client.request(method, self.prefix + path, body, headers)
            with client.getresponse() as response:
                data = response.read(65537) if response.status == 200 else None
                if data and len(data) <= 65536:
                    try:
                        return response.status, json_object(data)
                    except (ValueError, RecursionError):
                        pass
                return response.status, None
        except (OSError, HTTPException):
            return None, None
        finally:
            client.close()

    def submit(self, request):
        status, _ = self._request('POST', '/arena/v1/runs', request)
        return status

    def lookup(self, run_id):
        return self._request('GET', '/arena/v1/runs/' + quote(run_id, safe=''))

    def lookup_batch(self, run_ids):
        if not 1 <= len(run_ids) <= RUN_QUERY_BATCH_SIZE:
            raise ValueError('Arena 批量查询须包含 1 至 100 个执行编号')
        return self._request('POST', '/arena/v1/runs/query', {'run_ids': list(run_ids)})

    def status(self):
        _, payload = self._request('GET', '/arena/v1/status')
        return payload


def run_observation(run_id, payload):
    if not isinstance(payload, dict) or payload.get('run_id') != run_id:
        return None
    state = payload.get('state')
    if state == 'platform_error':
        if set(payload) == {'run_id', 'state', 'reason'} and payload['reason'] in PLATFORM_ERRORS:
            return {'state': state, 'reason': payload['reason']}
    elif state in (*ACTIVE_ARENA_STATES, 'finished', 'build_failed') and set(payload) == {'run_id', 'state'}:
        return {'state': state}
    return None


def batch_lookups(run_ids, payload):
    """Require a complete partition of the requested IDs before accepting a batch."""
    if not isinstance(payload, dict):
        return {}
    runs, missing = payload.get('runs'), payload.get('missing_run_ids')
    if not isinstance(runs, list) or not isinstance(missing, list):
        return {}
    remaining, lookups = set(run_ids), {}
    for run in runs:
        run_id = run.get('run_id') if isinstance(run, dict) else None
        if not isinstance(run_id, str) or run_id not in remaining:
            return {}
        remaining.remove(run_id)
        lookups[run_id] = (200, run)
    for run_id in missing:
        if not isinstance(run_id, str) or run_id not in remaining:
            return {}
        remaining.remove(run_id)
        lookups[run_id] = (404, None)
    return lookups if not remaining else {}
