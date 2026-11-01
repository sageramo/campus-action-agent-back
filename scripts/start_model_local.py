"""Start a loopback-only model service; read the app key without echo or saving."""
import argparse
import getpass
import os
from pathlib import Path
import socket
import sys
import warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from campus_agent.dify_client import extraction_service_status
from campus_agent.server import run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-base', default='https://api.dify.ai/v1', help='Dify application API base, not the published chat URL')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    with socket.socket() as probe:
        try:
            probe.bind(('127.0.0.1', args.port))
        except OSError:
            print('端口已被占用，请先停止原本地后端服务，再运行本命令；尚未读取密钥。')
            return 1
    if not sys.stdin.isatty():
        print('请在你自己的交互终端运行；密钥只通过隐藏输入读取。')
        return 1
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            app_key = getpass.getpass('粘贴新 Workflow 的应用 API Key（输入不显示、不保存到文件）：').strip()
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        print('\n无法安全读取隐藏输入或操作已取消；服务未启动。')
        return 1
    os.environ['DIFY_API_KEY'] = app_key
    os.environ['DIFY_API_BASE'] = args.api_base
    os.environ['SIXU_MODEL_EXTRACTION_ENABLED'] = 'true'
    if not extraction_service_status()['configured']:
        print('模型配置不完整，请核对地址、应用密钥和超时设置。')
        os.environ.pop('DIFY_API_KEY', None)
        return 1
    print('本地模型入口已启用；配置检查通过，尚未验证上游连通性。按 Ctrl+C 停止。')
    try:
        run(host='127.0.0.1', port=args.port)
    except KeyboardInterrupt:
        pass
    finally:
        os.environ.pop('DIFY_API_KEY', None)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
