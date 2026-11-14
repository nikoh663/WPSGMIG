#!/usr/bin/env bash
# Builds the disposable test environment for wpmigrate's end-to-end runs.
#   - two HTTPS source WordPress sites (src-a.test normal, src-b.test with DISALLOW_FILE_MODS)
#   - a shared-hosting-style destination web server serving ~wpuser/www/<domain>/public_html
#   - one MariaDB server per role
#   - an unprivileged local account "wpuser" that runs the tool
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REF=/opt/wpm-ref
NET=wpmnet
docker network inspect $NET >/dev/null 2>&1 || docker network create --subnet 172.30.0.0/16 $NET >/dev/null

run_db() { # name ip
  docker rm -f "$1" >/dev/null 2>&1 || true
  docker run -d --name "$1" --network $NET --ip "$2" \
    -e MARIADB_ROOT_PASSWORD=rootpw mariadb:10.11 >/dev/null
}
run_db wpm-src-db 172.30.0.11
run_db wpm-dest-db 172.30.0.12

mkdir -p /opt/wpm-fixtures && cp "$REF/reprint-wordpress-org.zip" /opt/wpm-fixtures/reprint.zip
cat > /opt/wpm-fixtures/alias.conf <<'CONF'
Alias /reprint-test-download.zip /fixtures/reprint.zip
<Directory /fixtures>
    Require ip 127.0.0.1
</Directory>
CONF

run_source() { # name ip host dbname extra-config
  docker rm -f "$1" >/dev/null 2>&1 || true
  docker run -d --name "$1" --network $NET --ip "$2" \
    -v "$HERE/certs:/certs:ro" -v /opt/wpm-fixtures:/fixtures:ro \
    -v "$HERE/source-ssl.conf:/etc/apache2/sites-enabled/source-ssl.conf:ro" \
    -v /opt/wpm-fixtures/alias.conf:/etc/apache2/conf-enabled/fixture-alias.conf:ro \
    -e WORDPRESS_DB_HOST=172.30.0.11 -e WORDPRESS_DB_USER=root -e WORDPRESS_DB_PASSWORD=rootpw \
    -e WORDPRESS_DB_NAME="$4" -e WORDPRESS_CONFIG_EXTRA="$5" \
    wordpress:php8.2-apache sh -c 'a2enmod ssl >/dev/null && docker-entrypoint.sh apache2-foreground' >/dev/null
}
run_source wpm-src-a 172.30.0.31 src-a.test src_a ""
run_source wpm-src-b 172.30.0.32 src-b.test src_b "define('DISALLOW_FILE_MODS', true);"

# Destination web server: serves the unprivileged user's ~/www tree.
id wpuser >/dev/null 2>&1 || useradd -m -s /bin/bash wpuser
mkdir -p /home/wpuser/www
chown wpuser:wpuser /home/wpuser/www
chmod 711 /home/wpuser
docker rm -f wpm-dest >/dev/null 2>&1 || true
docker run -d --name wpm-dest --network $NET --ip 172.30.0.20 \
  -v "$HERE/certs:/certs:ro" -v /home/wpuser/www:/srv/www:ro \
  -v "$HERE/dest-vhosts.conf:/etc/apache2/sites-enabled/000-default.conf:ro" \
  wordpress:php8.2-apache sh -c 'a2enmod ssl vhost_alias rewrite >/dev/null && apache2-foreground' >/dev/null

# Name resolution for the tool (host side).
sed -i '/# wpm-test$/d' /etc/hosts
cat >> /etc/hosts <<'HOSTS'
172.30.0.31 src-a.test # wpm-test
172.30.0.32 src-b.test # wpm-test
172.30.0.20 dest.test # wpm-test
172.30.0.20 old.dest.test # wpm-test
HOSTS
echo "environment containers started"
