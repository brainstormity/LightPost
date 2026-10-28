import os
import sys
from pathlib import Path

from aiohttp import web
from .app import create_app
from .config import Config
from .security import load_api_key


def main():
    args = sys.argv[1:]
    os.umask(0o077)
    if args == ["api-key"]:
        # Prints the key tools use to reach the API and MCP server.
        config = Config.from_env()
        print(config.api_key or load_api_key(Path(config.database).parent))
        return
    config = Config.from_env()
    web.run_app(
        create_app(config),
        host=config.host,
        port=config.port,
        access_log=None,
        print=lambda _: print(f"LightPost: http://{config.host}:{config.port}"),
    )


if __name__ == "__main__":
    main()
