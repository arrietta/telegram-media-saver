#!/bin/sh
# Сайты часто меняются, а образ собирается редко — поэтому при каждом запуске
# подтягиваем свежие загрузчики. Если PyPI недоступен, работаем на том, что есть.
timeout 120 pip install -q -U --no-cache-dir --root-user-action=ignore "yt-dlp[default]" gallery-dl \
    || echo "start.sh: не удалось обновить загрузчики, продолжаю со старыми"

exec python bot.py
