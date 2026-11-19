"""资源报文输入适配包。

`python -m src ...` 入口(见 src/cli.py)。
"""
from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
