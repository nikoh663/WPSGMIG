#!/usr/bin/env bash
# Installs WordPress on the two source containers and adds recognisable content.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
docker exec wpm-src-db mariadb -uroot -prootpw -e "CREATE DATABASE IF NOT EXISTS src_a; CREATE DATABASE IF NOT EXISTS src_b;"
for s in a b; do
  c=wpm-src-$s
  docker cp /opt/wpm-ref/wp-cli.phar $c:/usr/local/bin/wp
  docker exec $c chmod +x /usr/local/bin/wp
  W="docker exec -u www-data $c wp"
  $W core is-installed 2>/dev/null || $W core install --url=https://src-$s.test --title="Source Site ${s^^}" \
      --admin_user=admin --admin_password='Corr3ct-Horse!' --admin_email=admin@example.invalid --skip-email
  extra=""
  [ "$s" = b ] && extra="define( 'DISALLOW_FILE_MODS', true );"
  # Replace the image's environment-driven wp-config.php with an ordinary one.
  docker exec -u www-data $c sh -c "rm -f wp-config.php && printf '%s\n' \"$extra\" | wp config create --dbname=src_$s --dbuser=root --dbpass=rootpw --dbhost=172.30.0.11 --skip-check --extra-php" >/dev/null
  docker exec $c mkdir -p /var/www/html/wp-content/mu-plugins
  docker cp "$HERE/test-plugin-source.php" $c:/var/www/html/wp-content/mu-plugins/test-plugin-source.php
  docker exec $c chown -R www-data:www-data /var/www/html/wp-content
  docker cp "$HERE/htaccess-wp" $c:/var/www/html/.htaccess && docker exec $c chown www-data /var/www/html/.htaccess
  $W rewrite structure '/%postname%/' --hard >/dev/null
  $W user get editor >/dev/null 2>&1 || $W user create editor editor@example.invalid --role=editor --user_pass='Edit0r-Pass!' >/dev/null
  $W post create --post_status=publish --post_title="Hello from source ${s^^}" \
      --post_content="<p>Migrated content marker SRC-${s^^}-7731. Link: <a href=\"https://src-$s.test/sample-page/\">sample</a></p>" >/dev/null
  docker exec -u www-data $c sh -c "php -r '\$i=imagecreatetruecolor(40,40);imagepng(\$i,\"/tmp/marker.png\");' && wp media import /tmp/marker.png --title=marker >/dev/null"
  docker exec -u www-data $c sh -c "mkdir -p wp-content/themes/marker-theme && printf '/*\nTheme Name: Marker Theme\n*/\nbody{background:url(https://src-$s.test/wp-content/themes/marker-theme/bg.png)}\n' > wp-content/themes/marker-theme/style.css && printf '<?php wp_head(); ?><h1><?php bloginfo(\"name\"); ?></h1><?php while(have_posts()){the_post(); the_title(\"<h2>\",\"</h2>\"); the_content();} wp_footer();' > wp-content/themes/marker-theme/index.php"
  $W theme activate marker-theme >/dev/null
  # A largish file so that an interrupted download can be demonstrated.
  docker exec -u www-data $c sh -c "head -c 60000000 /dev/urandom > wp-content/uploads/large-video.bin"
done
echo seeded
