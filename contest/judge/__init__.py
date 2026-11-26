"""Run a single match and write the referee outputs."""
from engine import VERSION
from engine.match import Match
from engine.protocol import dumps


def run_match(board, bots, *, names, run_id, map_sha256, output):
    """Write the result, replay and referee log for a prepared match."""
    result = Match(board, bots, names=names, run_id=run_id, out_dir=output).run()
    result.update(version=VERSION, map_sha256=map_sha256)
    temporary = output / 'result.json.tmp'
    temporary.write_text(dumps(result) + '\n', encoding='utf-8')
    temporary.replace(output / 'result.json')
    return result
