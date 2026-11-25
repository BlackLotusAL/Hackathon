"""Execute an Arena task and publish the referee's result and observations."""
import time

from engine import VERSION
from engine.mapgen import load_map
from engine.protocol import RUN_ID, dumps, json_object
from engine.referee import HttpBot, Match
from engine.rules import RULESET


class ContainerBot(HttpBot):
    def __init__(self, url, marker):
        super().__init__(url)
        self.marker = marker
        self.exit_info = None

    def exited(self):
        if self.exit_info is None and self.marker.is_file():
            self.exit_info = json_object(self.marker.read_bytes())
        return self.exit_info is not None

    def request(self, method, path, body, limit):
        deadline = time.monotonic() + limit
        result = super().request(method, path, body, limit)
        if method == 'POST' and result[0] in ('connection', 'timeout'):
            # Arena writes the marker after Docker reports the exit. A broken
            # turn connection is already charged its entire time limit.
            is_turn = isinstance(body, dict) and body.get('type') == 'turn'
            if result[0] != 'connection' or not is_turn:
                deadline = time.monotonic() + 0.05
            while not self.exited():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.01, remaining))
        return result


def run_task(task_path, output, stage=None):
    task = json_object(task_path.read_bytes())
    run_id = task['run_id']
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError('run_id 必须是形如 run-7001.2 的完整执行编号')
    if (stage is not None and stage != RULESET) or task.get('rules', RULESET) != RULESET:
        raise ValueError('裁判只接受当前赛题规则（prelim）')
    board, digest = load_map(task['config']['map'])
    specs = task['bots']
    if not isinstance(specs, list) or len(specs) != 2:
        raise ValueError('bots 必须按座位顺序提供两项')
    bots = []
    names = []
    for seat, spec in enumerate(specs):
        url = spec['url']
        if not isinstance(url, str) or not url.startswith('http://'):
            raise ValueError('bot url 必须是 HTTP 服务根地址')
        name = spec.get('name', f'bot{seat}')
        if not isinstance(name, str):
            raise ValueError('Bot 名称须为字符串')
        names.append(name)
        bots.append(ContainerBot(url, task_path.parent / 'exits' / f'p{seat}.json'))
    result = Match(board, bots, names=names, run_id=run_id, out_dir=output).run()
    result.update(version=VERSION, map_sha256=digest)
    temporary = output / 'result.json.tmp'
    temporary.write_text(dumps(result) + '\n', encoding='utf-8')
    temporary.replace(output / 'result.json')
    return result
