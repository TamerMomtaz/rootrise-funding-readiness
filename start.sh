#!/bin/bash
pip install -r requirements.txt
# Single worker on purpose: SQLite + one process = no cross-process write contention.
exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'
