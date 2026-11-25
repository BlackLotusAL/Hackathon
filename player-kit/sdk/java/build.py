"""Package a flat Java submission with the local Maven and JDK."""
import os
from pathlib import Path
import shutil
import subprocess


def java_executable():
    home = os.environ.get('JAVA_HOME')
    name = 'java.exe' if os.name == 'nt' else 'java'
    java = Path(home) / 'bin' / name if home else shutil.which('java')
    if not java or not Path(java).is_file():
        raise ValueError('请安装 JDK 17，并设置 JAVA_HOME 或将 java 加入 PATH')
    return str(java)


def build_bot(directory, workspace):
    maven = shutil.which('mvn')
    if not maven:
        raise ValueError('请安装 Maven，并将 mvn 加入 PATH')
    sdk = Path(__file__).resolve().parent
    bot_sources = workspace / 'src/main/java/bot'
    sdk_sources = bot_sources / 'sdk'
    bot_sources.mkdir(parents=True)
    sdk_sources.mkdir()
    shutil.copyfile(directory / 'pom.xml', workspace / 'pom.xml')
    for source in directory.glob('*.java'):
        if source.is_file():
            shutil.copyfile(source, bot_sources / source.name)
    for name in ('Main.java', 'StateDecoder.java'):
        shutil.copyfile(sdk / name, bot_sources / name)
    for name in ('BoardTypes.java', 'GameMap.java', 'InitState.java', 'TurnState.java'):
        shutil.copyfile(sdk / name, sdk_sources / name)
    try:
        subprocess.run([maven, '--batch-mode', '--no-transfer-progress', '-DskipTests', 'package'],
                       cwd=workspace, check=True, timeout=600)
    except subprocess.SubprocessError as error:
        raise ValueError('Java 本机构建失败，请检查 Maven 输出和 JDK 配置') from error
    jar = workspace / 'target' / 'bot.jar'
    if not jar.is_file():
        raise ValueError('Maven 构建完成后未找到 target/bot.jar，请检查 pom.xml 的构建输出')
    return jar
