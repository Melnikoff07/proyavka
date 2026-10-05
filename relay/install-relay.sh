#!/usr/bin/env bash
# Настройка сервера-приёмника «Проявки» на чистом Ubuntu/Debian. Запускает мастер setup.py (под root).
#   bash install-relay.sh <файл-с-параметрами>
# Параметры (файл удаляется сразу после чтения):
#   DOMAIN        имя сервера (по умолчанию мастер берёт 1-2-3-4.sslip.io из IP)
#   PUBLIC_IP     внешний IP — для пассивного режима FTP
#   CAMERA_TOKEN  токен приложения камеры
#   FTP_PASS      пароль FTP-пользователя camera
#   LE_AGREE=1    пользователь согласился с условиями Let's Encrypt; LE_EMAIL — необязательно
#   SYNC_USER, SYNC_PUBKEY   режим «дом + сервер»: через кого домашний компьютер забирает кадры
#   BOT_USER      режим «всё на одном сервере»: пользователь, под которым работает бот
# Камеры приглашённых пользователей бот заводит сам через proyavka-user (sudo, только этот скрипт).
# Скрипт можно запускать повторно: он приводит сервер к нужному виду, ничего не ломая.
set -euo pipefail

if [ "${1:-}" ] && [ -f "$1" ]; then
    set -a; . "$1"; set +a
    rm -f "$1"
fi
: "${DOMAIN:?DOMAIN missing}" "${PUBLIC_IP:?PUBLIC_IP missing}" "${CAMERA_TOKEN:?CAMERA_TOKEN missing}" "${FTP_PASS:?FTP_PASS missing}"
SYNC_USER=${SYNC_USER:-proyavka}
UPLOAD_DIR=/srv/camera/upload
HERE=$(cd "$(dirname "$0")" && pwd)
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
t() { if [ "${LANGUAGE:-ru}" = en ]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }   # t "по-русски" "in English"

say "$(t "Пакеты" "Packages")"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq nginx vsftpd certbot inotify-tools rsync python3 curl libpam-pwdfile openssl sudo >/dev/null

say "$(t "Пользователи и папки" "Users and folders")"
getent group photos >/dev/null || groupadd photos
id camera >/dev/null 2>&1 || useradd -d /srv/camera -s /usr/sbin/nologin -g photos camera
grep -qx /usr/sbin/nologin /etc/shells || echo /usr/sbin/nologin >> /etc/shells   # иначе PAM не пустит в FTP
echo "camera:$FTP_PASS" | chpasswd
install -d -m 755 -o root -g root /srv/camera                     # корень chroot FTP не должен быть доступен на запись
install -d -m 2775 -o camera -g photos "$UPLOAD_DIR" "$UPLOAD_DIR/.incoming"
install -d -m 755 -o root -g root /srv/camera/u                   # папки приглашённых пользователей: u<id>/upload
install -d -m 755 -o root -g root /etc/proyavka
if [ -n "${SYNC_PUBKEY:-}" ]; then
    id "$SYNC_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$SYNC_USER"
    usermod -aG photos "$SYNC_USER"
    h=$(getent passwd "$SYNC_USER" | cut -d: -f6)
    install -d -m 700 -o "$SYNC_USER" -g "$SYNC_USER" "$h/.ssh"
    touch "$h/.ssh/authorized_keys"
    grep -qF "$SYNC_PUBKEY" "$h/.ssh/authorized_keys" || echo "$SYNC_PUBKEY" >> "$h/.ssh/authorized_keys"
    chown "$SYNC_USER:$SYNC_USER" "$h/.ssh/authorized_keys"; chmod 600 "$h/.ssh/authorized_keys"
fi
if [ -n "${BOT_USER:-}" ]; then usermod -aG photos "$BOT_USER"; fi

say "$(t "Сертификат HTTPS для" "HTTPS certificate for") $DOMAIN"
install -d /var/www/proyavka-acme
cat > /etc/nginx/sites-available/proyavka <<EOF
server {
    listen 80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/proyavka-acme; }
    location / { return 301 https://\$host\$request_uri; }
}
EOF
ln -sf /etc/nginx/sites-available/proyavka /etc/nginx/sites-enabled/proyavka
nginx -t && systemctl reload nginx
if [ ! -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    [ "${LE_AGREE:-}" = 1 ] || { echo "Let's Encrypt terms must be accepted (LE_AGREE=1)"; exit 1; }
    if [ -n "${LE_EMAIL:-}" ]; then mail=(--email "$LE_EMAIL"); else mail=(--register-unsafely-without-email); fi
    certbot certonly --webroot -w /var/www/proyavka-acme -d "$DOMAIN" --non-interactive --agree-tos "${mail[@]}"
fi
install -d /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/proyavka.sh <<'EOF'
#!/bin/sh
systemctl reload nginx
systemctl restart vsftpd
EOF
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/proyavka.sh

say "nginx"
install -d /etc/nginx/snippets
cat > /etc/nginx/snippets/proyavka-camera.conf <<'EOF'
location /camera/ {
    proxy_pass http://127.0.0.1:8089;
    proxy_http_version 1.1;
    proxy_request_buffering off;
    client_max_body_size 80m;
    client_body_timeout 120s;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
EOF
cat >> /etc/nginx/sites-available/proyavka <<EOF
server {
    listen 443 ssl;
    http2 on;
    server_name $DOMAIN;
    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    client_max_body_size 1m;
    include snippets/proyavka-camera.conf;
    location = /api/upload {          # «+» в «Проявке»: свои фото с телефона, файл идёт в бота потоком
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host \$host;
        proxy_http_version 1.1;
        proxy_request_buffering off;
        client_max_body_size 50m;
        client_body_timeout 120s;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host \$host;
        proxy_read_timeout 120s;
    }
}
EOF
if ! nginx -t 2>/dev/null; then   # старый nginx не знает "http2 on"
    sed -i -e 's/listen 443 ssl;/listen 443 ssl http2;/' -e '/http2 on;/d' /etc/nginx/sites-available/proyavka
fi
nginx -t && systemctl reload nginx

say "$(t "FTP (для камер со встроенной отправкой по FTP)" "FTP (for cameras with built-in FTP upload)")"
[ -f /etc/vsftpd.conf.before-proyavka ] || cp -a /etc/vsftpd.conf /etc/vsftpd.conf.before-proyavka
# Логины FTP — виртуальные (pam_pwdfile): у каждого пользователя бота свой вход и своя папка, все пишут от имени camera.
# camera — камера администратора, папка /srv/camera (в ней upload), как в прежних версиях.
touch /etc/proyavka/ftp.passwd && chown root:root /etc/proyavka/ftp.passwd && chmod 600 /etc/proyavka/ftp.passwd
grep -v '^camera:' /etc/proyavka/ftp.passwd > /etc/proyavka/ftp.passwd.new || true
printf 'camera:%s\n' "$(printf '%s\n' "$FTP_PASS" | openssl passwd -6 -stdin)" >> /etc/proyavka/ftp.passwd.new
chmod 600 /etc/proyavka/ftp.passwd.new && mv -f /etc/proyavka/ftp.passwd.new /etc/proyavka/ftp.passwd
touch /etc/proyavka/camera-tokens && chown root:photos /etc/proyavka/camera-tokens && chmod 640 /etc/proyavka/camera-tokens
install -d -m 755 /etc/vsftpd/proyavka-users
echo "local_root=/srv/camera" > /etc/vsftpd/proyavka-users/camera
cat > /etc/pam.d/vsftpd-proyavka <<'EOF'
# «Проявка»: вход камер по FTP — только логины из /etc/proyavka/ftp.passwd
auth    required pam_pwdfile.so pwdfile=/etc/proyavka/ftp.passwd
account required pam_permit.so
EOF
cat > /etc/vsftpd.conf <<EOF
# «Проявка»: FTPS для камер. Прежний файл — /etc/vsftpd.conf.before-proyavka
listen=YES
listen_ipv6=NO
anonymous_enable=NO
local_enable=YES
write_enable=YES
local_umask=002
chroot_local_user=YES
userlist_enable=YES
userlist_deny=NO
userlist_file=/etc/vsftpd.userlist
pam_service_name=vsftpd-proyavka
guest_enable=YES
guest_username=camera
virtual_use_local_privs=YES
user_config_dir=/etc/vsftpd/proyavka-users
seccomp_sandbox=NO
xferlog_enable=YES
pasv_enable=YES
pasv_min_port=50000
pasv_max_port=50100
pasv_address=$PUBLIC_IP
ssl_enable=YES
rsa_cert_file=/etc/letsencrypt/live/$DOMAIN/fullchain.pem
rsa_private_key_file=/etc/letsencrypt/live/$DOMAIN/privkey.pem
force_local_logins_ssl=YES
force_local_data_ssl=YES
ssl_sslv2=NO
ssl_sslv3=NO
require_ssl_reuse=NO
ssl_ciphers=HIGH
EOF
grep -qx camera /etc/vsftpd.userlist 2>/dev/null || echo camera >> /etc/vsftpd.userlist
systemctl enable vsftpd >/dev/null 2>&1
systemctl restart vsftpd

say "$(t "Приёмник для приложения камеры" "Receiver for the camera app")"
install -d -m 755 /usr/local/lib/proyavka
install -m 755 "$HERE/camera-recv.py" /usr/local/lib/proyavka/camera-recv.py
install -m 755 -o root -g root "$HERE/proyavka-user" /usr/local/lib/proyavka/proyavka-user
umask 077
printf 'CAMERA_TOKEN=%s\nUPLOAD_DIR=%s\nPORT=8089\nTOKENS_FILE=/etc/proyavka/camera-tokens\nUSERS_DIR=/srv/camera/u\n' \
    "$CAMERA_TOKEN" "$UPLOAD_DIR" > /etc/proyavka/camera-recv.env
umask 022
# бот заводит камеры приглашённых пользователей только через этот скрипт — больше sudo ему ничего не даёт
sudoers=""
if [ -n "${SYNC_PUBKEY:-}" ]; then sudoers="$SYNC_USER ALL=(root) NOPASSWD: /usr/local/lib/proyavka/proyavka-user"$'\n'; fi
if [ -n "${BOT_USER:-}" ]; then sudoers="$sudoers$BOT_USER ALL=(root) NOPASSWD: /usr/local/lib/proyavka/proyavka-user"$'\n'; fi
if [ -n "$sudoers" ]; then
    printf '%s' "$sudoers" > /etc/sudoers.d/proyavka.new
    chmod 440 /etc/sudoers.d/proyavka.new
    if visudo -cf /etc/sudoers.d/proyavka.new >/dev/null; then mv -f /etc/sudoers.d/proyavka.new /etc/sudoers.d/proyavka
    else rm -f /etc/sudoers.d/proyavka.new; echo "sudoers check failed" >&2; fi
fi
cat > /etc/systemd/system/proyavka-recv.service <<EOF
[Unit]
Description=Proyavka: camera upload receiver
After=network.target

[Service]
User=camera
Group=photos
EnvironmentFile=/etc/proyavka/camera-recv.env
ExecStart=/usr/bin/python3 /usr/local/lib/proyavka/camera-recv.py
Restart=always
RestartSec=3
UMask=0002
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/srv/camera
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable proyavka-recv >/dev/null 2>&1
systemctl restart proyavka-recv

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    say "$(t "Файрвол" "Firewall")"
    ufw allow 80/tcp; ufw allow 443/tcp; ufw allow 21/tcp; ufw allow 50000:50100/tcp
fi

sleep 1
code=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Token: $CAMERA_TOKEN" "https://$DOMAIN/camera/ping" || true)
say "$(t "Готово" "Done")"
echo "$(t "проверка" "check") https://$DOMAIN/camera/ping: $code ($(t "должно быть" "expected") 200)"
[ "$code" = 200 ]
