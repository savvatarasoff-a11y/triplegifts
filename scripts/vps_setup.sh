#!/usr/bin/env bash
# Установка Triple Gifts на чистый Ubuntu 22.04/24.04 VPS.
#   bash vps_setup.sh [путь к triple-gifts.bundle]   (по умолчанию ищет бандл рядом со скриптом или в /root)
# Спросит токен бота (не сохраняется в истории), поставит Python + Caddy (HTTPS через sslip.io),
# запустит бота как systemd-сервис и включит резервные копии базы каждые 6 часов.
# Повторный запуск обновляет код и перезапускает бота; база не трогается.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BUNDLE="${1:-}"
for b in "$HERE/triple-gifts.bundle" /root/triple-gifts.bundle; do
  if [ -z "$BUNDLE" ] && [ -f "$b" ]; then BUNDLE="$b"; fi
done
APP=/opt/triple-gifts
DATA=/var/lib/triple-gifts
ENV_FILE=/etc/triple-gifts.env

[ "$(id -u)" = 0 ] || { echo "Запусти от root"; exit 1; }

IP="$(curl -4fsS https://api.ipify.org || hostname -I | awk '{print $1}')"
DOMAIN="${DOMAIN:-${IP//./-}.sslip.io}"
echo "Адрес мини-приложения: https://$DOMAIN"

echo "== Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip sqlite3 git curl gpg >/dev/null
if ! apt-get install -y -q caddy >/dev/null 2>&1; then   # в 22.04 caddy нет в стандартных репозиториях
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy.gpg
  echo "deb [signed-by=/usr/share/keyrings/caddy.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" > /etc/apt/sources.list.d/caddy.list
  apt-get update -q && apt-get install -y -q caddy >/dev/null
fi

# код (и снимок базы, если есть) берём из git-бандла; без бандла — из папки, где лежит скрипт
SRC="$(cd "$HERE/.." && pwd)"
if [ -n "$BUNDLE" ]; then
  echo "== Распаковываю $BUNDLE"
  SRC=/root/triple-src; rm -rf "$SRC"; git init -q "$SRC"
  git -C "$SRC" fetch -q "$BUNDLE" "refs/heads/*:refs/heads/*" "refs/remotes/origin/data:refs/heads/data" 2>/dev/null \
    || git -C "$SRC" fetch -q "$BUNDLE" "refs/heads/*:refs/heads/*"
  BR=claude/telegram-bot-python-oxklc7
  git -C "$SRC" rev-parse -q --verify "refs/heads/$BR" >/dev/null || BR=main
  git -C "$SRC" checkout -q "$BR"
  git -C "$SRC" show data:bot.db > "$SRC/bot.db" 2>/dev/null || rm -f "$SRC/bot.db"
fi

echo "== Код"
mkdir -p "$APP" "$DATA/backups"
cp -r "$SRC/app" "$SRC/webapp" "$SRC/requirements.txt" "$APP/"
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

if [ ! -f "$DATA/bot.db" ] && [ -f "$SRC/bot.db" ]; then
  echo "== Восстанавливаю базу из архива"
  cp "$SRC/bot.db" "$DATA/bot.db"
fi

if [ ! -f "$ENV_FILE" ]; then
  read -rsp "Вставь токен бота (@TripleGifts_bot) и нажми Enter: " TOKEN; echo
  [ -n "$TOKEN" ] || { echo "Токен пустой"; exit 1; }
  umask 077
  cat > "$ENV_FILE" <<EOF
BOT_TOKEN=$TOKEN
WEBAPP_URL=https://$DOMAIN
DB_PATH=$DATA/bot.db
PORT=8080
EOF
  unset TOKEN
fi
chmod 600 "$ENV_FILE"

echo "== Сервис"
cat > /etc/systemd/system/triple-gifts.service <<EOF
[Unit]
Description=Triple Gifts bot
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python -m app.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:8080
}
EOF

# резервная копия базы каждые 6 часов, храним 2 недели
cat > /etc/cron.d/triple-gifts-backup <<EOF
0 */6 * * * root sqlite3 $DATA/bot.db ".backup '$DATA/backups/bot-\$(date +\%Y\%m\%d-\%H\%M).db'" && find $DATA/backups -name 'bot-*.db' -mtime +14 -delete
EOF

systemctl daemon-reload
systemctl enable --now triple-gifts >/dev/null
systemctl restart triple-gifts caddy

sleep 8
if systemctl is-active --quiet triple-gifts; then
  echo "✅ Бот запущен. Мини-приложение: https://$DOMAIN"
  echo "Логи: journalctl -u triple-gifts -f"
else
  echo "❌ Бот не стартовал, последние строки лога:"
  journalctl -u triple-gifts -n 40 --no-pager
  exit 1
fi
