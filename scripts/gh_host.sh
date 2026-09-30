#!/usr/bin/env bash
# Временный хостинг на GitHub Actions: бот + мини-приложение через туннель Cloudflare.
# База (зашифрованная) сохраняется в ветку `data-enc` каждые 30 секунд и восстанавливается при следующем запуске.
set -uo pipefail

RUN_SECONDS="${RUN_SECONDS:-20400}"      # 5 ч 40 мин, лимит задачи Actions — 6 ч
BACKUP_EVERY="${BACKUP_EVERY:-30}"
DATA_DIR="$PWD/data"
REMOTE="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
mkdir -p "$DATA_DIR"

if [ -z "${BOT_TOKEN:-}" ]; then
  echo "::error::Нет секрета BOT_TOKEN. Добавьте его: Settings → Secrets and variables → Actions → New repository secret"
  exit 1
fi

# 1. Восстановить базу. Репозиторий публичный, поэтому база в ветке `data-enc` зашифрована
#    (AES-256, ключ — секрет DB_KEY, а если его нет — BOT_TOKEN). Старая ветка `data` — незашифрованная,
#    читается один раз для переезда и удаляется после первого зашифрованного снимка.
ENC_BRANCH="data-enc"
DB_PASS="${DB_KEY:-$BOT_TOKEN}"
export DB_PASS
[ -z "${DB_KEY:-}" ] && echo "::warning::Секрета DB_KEY нет — база шифруется токеном бота. Добавьте DB_KEY, чтобы смена токена не мешала расшифровке."
decrypt() {  # $1 — зашифрованный файл, $2 — пароль
  DEC_PASS="$2" openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:DEC_PASS -in "$1" -out "$DATA_DIR/bot.db" 2>/dev/null
}
LEGACY=""
if git fetch -q origin "$ENC_BRANCH" 2>/dev/null && git show "origin/$ENC_BRANCH:bot.db.enc" > "$DATA_DIR/bot.db.enc" 2>/dev/null; then
  if decrypt "$DATA_DIR/bot.db.enc" "$DB_PASS" || { [ -n "${DB_KEY:-}" ] && decrypt "$DATA_DIR/bot.db.enc" "$BOT_TOKEN"; }; then
    echo "База восстановлена и расшифрована ($(stat -c %s "$DATA_DIR/bot.db") байт)"
  else
    echo "::error::Не удалось расшифровать базу (сменился DB_KEY/BOT_TOKEN?). Бот не запускаю, чтобы не затереть базу пустой."
    exit 1
  fi
  rm -f "$DATA_DIR/bot.db.enc"
elif git fetch -q origin data 2>/dev/null && git show origin/data:bot.db > "$DATA_DIR/bot.db" 2>/dev/null; then
  LEGACY=1
  echo "База восстановлена из старой ветки data ($(stat -c %s "$DATA_DIR/bot.db") байт) — перевожу на шифрование"
else
  rm -f "$DATA_DIR/bot.db"
  echo "Сохранённой базы нет — начинаем с пустой"
fi

backup() {
  python scripts/backup_db.py "$DATA_DIR/bot.db" "$DATA_DIR/snapshot.db" || return 1
  local tmp
  tmp=$(mktemp -d)
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:DB_PASS \
    -in "$DATA_DIR/snapshot.db" -out "$tmp/bot.db.enc" || { rm -rf "$tmp"; return 1; }
  (
    cd "$tmp" && git init -q && git checkout -q -b "$ENC_BRANCH" && git add bot.db.enc &&
    git -c user.name="triple-bot" -c user.email="triple-bot@users.noreply.github.com" commit -qm "Снимок базы (зашифрован) $(date -u +%FT%TZ)" &&
    git push -qf "$REMOTE" "$ENC_BRANCH"
  ) && echo "Снимок базы сохранён $(date -u +%T)" || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
  # переезд завершён — незашифрованную ветку удаляем
  if [ -n "$LEGACY" ]; then
    git push -q "$REMOTE" --delete data 2>/dev/null && echo "Старая незашифрованная ветка data удалена"
    LEGACY=""
  fi
}

# сразу после запуска — первый зашифрованный снимок (и удаление старой ветки)
[ -f "$DATA_DIR/bot.db" ] && [ -n "$LEGACY" ] && backup

# 2. Туннель для мини-приложения
curl -fsSL -o cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x cloudflared
./cloudflared tunnel --no-autoupdate --url http://localhost:8080 > tunnel.log 2>&1 &
TUNNEL_PID=$!
URL=""
for _ in $(seq 60); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' tunnel.log | head -1)
  [ -n "$URL" ] && break
  sleep 1
done
if [ -z "$URL" ]; then
  echo "::warning::Туннель не поднялся, бот запустится без мини-приложения"
fi
echo "Мини-приложение: ${URL:-нет}"

# 3. Бот
export WEBAPP_URL="$URL" DB_PATH="$DATA_DIR/bot.db" PORT=8080
python -m app.main &
BOT_PID=$!

END=$((SECONDS + RUN_SECONDS))
STOP=""
# При отмене запуска (Cancel run) GitHub присылает SIGINT/SIGTERM — сохраняем базу перед выходом
trap 'STOP=1' INT TERM
CHAINED=""
STARTED=$SECONDS
while [ -z "$STOP" ] && [ $SECONDS -lt $END ] && kill -0 $BOT_PID 2>/dev/null; do
  sleep "$BACKUP_EVERY" & wait $!
  [ -n "$STOP" ] && break
  [ -f "$DATA_DIR/bot.db" ] && backup
  # Бот живёт 2 минуты — ставим в очередь следующий запуск (он дождётся окончания этого)
  if [ -z "$CHAINED" ] && [ $((SECONDS - STARTED)) -ge 120 ] && [ -n "${CHAIN_WORKFLOW:-}" ]; then
    gh workflow run "$CHAIN_WORKFLOW" -R "$GITHUB_REPOSITORY" --ref main && CHAINED=1 && echo "Следующий запуск в очереди"
  fi
done

# 4. Корректная остановка: следующий запуск уже ждёт в очереди.
# Сначала снимок базы (SQLite backup API работает на живой базе) — после отмены у нас всего несколько секунд
[ -n "$STOP" ] && [ -f "$DATA_DIR/bot.db" ] && backup
echo "Останавливаю бота"
kill -TERM $BOT_PID 2>/dev/null
wait $BOT_PID
CODE=$?
kill $TUNNEL_PID 2>/dev/null
[ -f "$DATA_DIR/bot.db" ] && backup
exit $([ $SECONDS -ge $END ] && echo 0 || echo $CODE)
