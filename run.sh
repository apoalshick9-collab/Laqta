#!/bin/bash
# تشغيل سيرفر لقطة محلياً
cd "$(dirname "$0")"
exec python3 -m uvicorn server:app --host 0.0.0.0 --port 8000
