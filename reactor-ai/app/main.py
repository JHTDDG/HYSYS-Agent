"""FastAPI 入口。

本地启动：  python -m app.main 或 python app/main.py   （默认 http://127.0.0.1:8000）
远程部署：  HOST=0.0.0.0 python -m app.main
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 允许直接运行本文件（python app/main.py）：自举为包模块后再做相对导入
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "app"

from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import service
from .schema import dump_model

app = FastAPI(title="AI 化工反应器智能建模", version="0.1.0")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class TextInput(BaseModel):
    text: str
    save_case: Optional[bool] = None  # None=跟随服务端默认（HYSYS_SAVE_CASE），True/False 强制覆盖


@app.get("/api/status")
def api_status():
    return service.status()


@app.post("/api/parse")
def api_parse(inp: TextInput):
    tasks, used_llm = service.parse_text(inp.text)
    return {
        "used_llm": used_llm,
        "tasks": [dump_model(t) for t in tasks],
    }


@app.post("/api/simulate")
def api_simulate(inp: TextInput):
    return service.simulate(inp.text, save_case=inp.save_case)


@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
