"""Execute an Arena task and publish the referee's result and observations."""
import time

from engine import VERSION
from engine.mapgen import load_map
from engine.protocol import RUN_ID, dumps, json_object
from engine.referee import HttpBot, Match


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
        result = super().request(method, path, body, limit)
        if method == 'POST' and result[0] in ('connection', 'timeout'):
            # Arena publishes the exit marker just after Docker observes the exit.
            for _ in range(5):
                if self.exited():
                    break
                time.sleep(0.01)
        return result


def run_task(task_path, output, stage=None):
    task = json_object(task_path.read_bytes())
    run_id = task['run_id']
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError('run_id 必须是形如 run-7001.2 的完整执行编号')
    if stage is None:
        stage = task.get('rules', 'prelim')
    if stage not in ('prelim', 'final'):
        raise ValueError('赛段必须为 prelim 或 final')
    board, digest = load_map(task['config']['map'], stage)
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
    result = Match(board, bots, names=names, stage=stage, run_id=run_id, out_dir=output).run()
    result.update(version=VERSION, map_sha256=digest)
    temporary = output / 'result.json.tmp'
    temporary.write_text(dumps(result) + '\n', encoding='utf-8')
    temporary.replace(output / 'result.json')
    return result
