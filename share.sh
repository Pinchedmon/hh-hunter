#!/bin/sh
# Собирает hh-hunter.zip для другого человека: только код, без data/ (ключи, вход в hh, профиль, история)
set -e
cd "$(dirname "$0")"
rm -f hh-hunter.zip
zip -rq hh-hunter.zip . \
  -x 'data/*' '.venv/*' '*__pycache__*' '.git/*' 'hh-hunter.zip' '.DS_Store' '*.pyc'
# страховка: в архиве не должно быть следов личных файлов
if unzip -l hh-hunter.zip | grep -E 'data/|\.db$|profile\.md|prefs\.md|chrome-profile' ; then
  echo "СТОП: в архив попало личное"; rm hh-hunter.zip; exit 1
fi
echo "Готово: $(pwd)/hh-hunter.zip"
