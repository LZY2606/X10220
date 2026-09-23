"""NMR 共振经纬工作台 — 服务入口。

演示命令: .venv/bin/uvicorn app:app --host 127.0.0.1 --port 5340
"""
from nmrworkbench.api import create_app

app = create_app()
