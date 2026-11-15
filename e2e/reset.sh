#!/usr/bin/env bash
# Puts the test environment back to a known state between scenarios.
#   reset.sh            empty destination, fresh databases, no saved progress
#   reset.sh old-site   same, plus an existing WordPress site at dest.test
#   reset.sh old-site-sub  same, but that site is in the subfolder /new
set -euo pipefail
DB="docker exec wpm-dest-db mariadb -uroot -prootpw"
rm -rf /home/wpuser/.wpmigrate /home/wpuser/www/*
mkdir -p /home/wpuser/www/dest.test/public_html
for db in site_new site_old site_two; do
  $DB -e "DROP DATABASE IF EXISTS $db; CREATE DATABASE $db;"
done
$DB -e "CREATE USER IF NOT EXISTS 'site_new'@'%' IDENTIFIED BY 'New-Db-Pass-1'; GRANT ALL ON site_new.* TO 'site_new'@'%';
        CREATE USER IF NOT EXISTS 'site_old'@'%' IDENTIFIED BY 'Old-Db-Pass-2'; GRANT ALL ON site_old.* TO 'site_old'@'%';
        GRANT ALL ON site_two.* TO 'site_new'@'%'; FLUSH PRIVILEGES;"
for s in a b; do
  docker exec -u www-data wpm-src-$s sh -c 'wp plugin delete reprint >/dev/null 2>&1; wp option delete reprint_server_public_keys >/dev/null 2>&1; wp option delete reprint_server_connection_token >/dev/null 2>&1; true'
done
if [ "${1:-}" = "old-site" ] || [ "${1:-}" = "old-site-sub" ]; then
  SUB=""; [ "${1:-}" = "old-site-sub" ] && SUB="/new"
  # An existing WordPress site in the destination folder, with its own database.
  T=/home/wpuser/www/dest.test/public_html$SUB
  mkdir -p $T
  docker run --rm -v /home/wpuser/www:/out wordpress:php8.2-apache sh -c "cp -a /usr/src/wordpress/. /out/dest.test/public_html$SUB/"
  cp /home/user/wpm/e2e/env/htaccess-wp $T/.htaccess
  [ -n "$SUB" ] && sed -i "s#RewriteBase /#RewriteBase $SUB/#; s#\. /index.php#. $SUB/index.php#" $T/.htaccess
  echo '<html><body>google-site-verification</body></html>' > $T/google1234.html
  chown -R wpuser:wpuser /home/wpuser/www
  W="runuser -u wpuser -- php /opt/wpm-ref/wp-cli.phar --path=$T"
  $W config create --dbname=site_old --dbuser=site_old --dbpass=Old-Db-Pass-2 --dbhost=172.30.0.12 --skip-check >/dev/null
  $W core install --url=https://dest.test$SUB --title="Old Destination Site" --admin_user=oldadmin --admin_password=Old-Admin-9 --admin_email=old@example.invalid --skip-email >/dev/null
  $W post create --post_status=publish --post_title="Old site post" --post_content="OLD-DEST-MARKER-5512 <a href=\"https://dest.test$SUB/old-page/\">x</a>" >/dev/null
  $W rewrite structure '/%postname%/' >/dev/null
fi
chown -R wpuser:wpuser /home/wpuser/www
echo "reset done ${1:-}"
