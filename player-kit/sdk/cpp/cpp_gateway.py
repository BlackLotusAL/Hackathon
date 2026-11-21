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


def optional(value, encode):
    return b'0\n' if value is None else b'1\n' + encode(value)


def point(value):
    return numbers(value.x, value.y)


def rect(value):
    return numbers(value.x0, value.y0, value.x1, value.y1)


def beam(value):
    if value.dir not in ('U', 'D', 'L', 'R') or type(value.alive) is not bool:
        raise ValueError('invalid beam direction or alive flag')
    return (numbers(value.seat, value.x, value.y) + text(value.dir)
            + numbers(value.speed, int(value.alive), value.restart_in, value.boost_left))


def init_payload(state):
    if state.seat not in (0, 1) or not 1 <= state.size <= 41:
        raise ValueError('invalid seat or board size')
    return b''.join((
        b'I\n', numbers(state.seat, state.size, state.max_turns), text(state.stage), text(state.map),
        sequence(state.terrain, text), pair(state.calib, point),
        sequence(state.orders, rect), sequence(state.boost_points, point),
    ))


def turn_payload(state, latent):
    return b''.join((
        b'T\n', numbers(state.turn),
        sequence(state.owner, text), sequence(state.contour, text), sequence(latent, text),
        pair(state.beams, beam), pair(state.score, numbers), pair(state.bonus, numbers),
        optional(state.order, lambda order: numbers(order.no) + rect(order.rect) + numbers(order.until)),
        optional(state.boost, point),
        optional(state.boost_next, lambda boost: numbers(boost.x, boost.y, boost.at)),
    ))


class NativeStrategy:
    def __init__(self, binary, directory=None):
        self.binary = binary
        self.directory = directory
        self.process = None
        self.replies = None
        self.init = None
        self.init_message = None
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
            self.init, self.init_message = state, msg
        elif kind == 'turn':
            if self.process is None:
                raise RuntimeError('native strategy is not initialized')
            state = TurnState.from_message(self.init, self.init_message, msg)
            return {'turn': state.turn, 'cmd': self.exchange(turn_payload(state, msg.get('latent', ())))}
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
