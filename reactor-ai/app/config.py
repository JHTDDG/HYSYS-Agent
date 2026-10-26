"""极简 .env 配置加载（不引入额外依赖）。

项目根目录下的 .env 文件会在服务启动时自动加载；
已存在的环境变量优先于 .env 中的值，方便临时覆盖。
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: str | None = None) -> bool:
    p = Path(path) if path else ROOT / ".env"
    if not p.exists():
        return False
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
    return True
