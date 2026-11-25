"""Start the HTTP service for a C++ Bot."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread

from ..python import InitState, TurnState
from ..python.http import run


def numbers(*values):
    if any(type(value) is not int or not -(1 << 31) <= value < (1 << 31) for value in values):
        raise ValueError('expected 32-bit integers in strategy state')
    return (' '.join(map(str, values)) + '\n').encode('ascii')


def text(value):
    data = value.encode('utf-8')
    return numbers(len(data)) + data + b'\n'


def sequence(values, encode):
    return numbers(len(values)) + b''.join(map(encode, values))


def pair(values, encode):
    left, right = values
    return encode(left) + encode(right)


def point(value):
    return numbers(value.x, value.y)


def boost_zone(value):
    return point(value.marker) + sequence(value.cells, point)


def beam(value):
    if value.dir not in ('U', 'D', 'L', 'R') or type(value.alive) is not bool:
        raise ValueError('invalid beam direction or alive flag')
    return (numbers(value.seat, value.x, value.y) + text(value.dir)
            + numbers(value.speed, int(value.alive), value.restart_in))


def init_payload(state):
    board = state.map
    return b''.join((
        b'I\n', numbers(state.seat, state.max_turns), numbers(board.size), text(board.stage), text(board.id),
        sequence(board.terrain, text), pair(board.calib, point), sequence(board.boost_zones, boost_zone),
    ))


def turn_payload(state):
    return b''.join((
        b'T\n', numbers(state.turn),
        sequence(state.owner, text), sequence(state.contour, text),
        pair(state.beams, beam), pair(state.score, numbers),
        pair(state.lead_turns, numbers), pair(state.time_us, numbers),
    ))


class NativeStrategy:
    def __init__(self, binary, directory=None):
        self.binary = binary
        self.directory = directory
        self.process = None
        self.replies = None
        self.init = None
        self.closing = Event()

    def start(self):
        read_fd, write_fd = os.pipe()
        self.replies = os.fdopen(read_fd, 'rb')
        # Inherit a separate reply pipe so strategy stdout and stderr remain available for logging.
        try:
            if os.name == 'nt':
                from msvcrt import get_osfhandle

                handle = get_osfhandle(write_fd)
                os.set_handle_inheritable(handle, True)
                reply_pipe = handle
                options = {'startupinfo': subprocess.STARTUPINFO(lpAttributeList={'handle_list': [handle]})}
            else:
                reply_pipe = write_fd
                options = {'pass_fds': (write_fd,)}
            self.process = subprocess.Popen([self.binary, str(reply_pipe)], cwd=self.directory,
                                            stdin=subprocess.PIPE, close_fds=True, **options)
        except BaseException:
            self.replies.close()
            raise
        finally:
            os.close(write_fd)
        Thread(target=self.watch_exit, args=(self.process,), daemon=True).start()

    def watch_exit(self, process):
        code = process.wait()
        if not self.closing.is_set():
            print(f'native strategy exited with code {code}', file=sys.stderr, flush=True)
            os._exit(1)

    def exchange(self, payload):
        try:
            self.process.stdin.write(payload)
            self.process.stdin.flush()
            response = self.replies.readline(8)
        except BrokenPipeError:
            response = b''
        if not response:
            # The referee detects native crashes when the gateway exits.
            print('native strategy exited before replying', file=sys.stderr, flush=True)
            os._exit(1)
        if not response.endswith(b'\n'):
            raise RuntimeError('invalid reply frame from native strategy')
        return response[:-1].decode('ascii')

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        self.closing.set()
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        finally:
            self.replies.close()
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def handle(self, msg):
        kind = msg['type']
        if kind == 'init':
            state = InitState.from_message(msg)
            payload = init_payload(state)
            self.start()
            try:
                if self.exchange(payload) != 'OK':
                    raise RuntimeError('native strategy initialization failed')
            except Exception:
                self.close()
                raise
            self.init = state
        elif kind == 'turn':
            if self.process is None:
                raise RuntimeError('native strategy is not initialized')
            state = TurnState.from_message(self.init, msg)
            return {'turn': state.turn, 'cmd': self.exchange(turn_payload(state))}
        else:
            self.close()
        return None


def main():
    parser = argparse.ArgumentParser(description='启动 C++ 策略的 Bot 服务')
    parser.add_argument('binary', type=Path, help='编译后的策略程序')
    parser.add_argument('--cwd', type=Path, help='运行策略的提交目录')
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    run(runner_factory=lambda: NativeStrategy(binary, args.cwd), name='cpp')


if __name__ == '__main__':
    main()
