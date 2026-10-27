#!/bin/sh
# Запуск: ./run.sh — поднимает программу на http://127.0.0.1:5188 и открывает её в браузере
cd "$(dirname "$0")"
lsof -ti tcp:5188 2>/dev/null | xargs kill 2>/dev/null
exec uv run hunter
