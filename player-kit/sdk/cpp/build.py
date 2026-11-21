"""Compile a Bot with the local CMake and C++ toolchain."""
import os
from pathlib import Path
import shutil
import subprocess


def build_bot(directory, workspace):
    cmake = shutil.which('cmake')
    if not cmake:
        raise ValueError('请安装 CMake 3.16+ 和 C++17 编译器，并将工具加入 PATH')
    source = Path(__file__).resolve().parent
    build = workspace / 'build'
    output = workspace / 'bin'
    configure = [cmake, '-S', str(source), '-B', str(build), '-DCMAKE_BUILD_TYPE=Release',
                 f'-DBOT_SUBMISSION={directory}', f'-DBOT_OUTPUT_DIR={output}']
    if (os.name == 'nt' and not os.environ.get('CMAKE_GENERATOR')
            and not shutil.which('cl') and shutil.which('g++') and shutil.which('mingw32-make')):
        configure += ['-G', 'MinGW Makefiles']
    try:
        subprocess.run(configure, check=True, timeout=600)
        subprocess.run([cmake, '--build', str(build), '--config', 'Release', '--target', 'bot'],
                       check=True, timeout=600)
    except subprocess.SubprocessError as error:
        raise ValueError('C++ 本机构建失败，请检查 CMake 输出和本机编译工具链') from error
    binary = output / ('bot.exe' if os.name == 'nt' else 'bot')
    if not binary.is_file():
        raise ValueError('CMake 构建完成后未找到 Bot 可执行文件')
    return binary
