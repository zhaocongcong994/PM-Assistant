#!/usr/bin/env python3
"""Start the local workbench with the project's installed Python environment."""

import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
interpreters = [root / ".venv" / "bin" / "python", root / ".venv-local" / "bin" / "python", Path("/opt/homebrew/bin/python3.10")]
for interpreter in interpreters:
    if interpreter.is_file() and os.access(interpreter, os.X_OK):
        os.execv(str(interpreter), [str(interpreter), str(root / "web_app.py")])
sys.exit("未找到 Python 3.10+ 项目环境，请先运行 ./setup.sh。")
