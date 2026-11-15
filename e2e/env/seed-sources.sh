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
# A page, a menu and stored links, to check how addresses are rewritten.
docker exec wpm-src-a sh -c 'cd /var/www/html && wp --allow-root post list --post_type=page --name=reviews --format=count | grep -q 1 || {
  wp --allow-root post create --post_type=page --post_status=publish --post_title=Reviews --post_name=reviews --post_content="<!-- wp:paragraph --><p>See <a href=\"https://src-a.test/sample-page/\">sample</a> and <a href=\"https://src-a.test/reviews/\">reviews</a></p><!-- /wp:paragraph -->" &&
  wp --allow-root menu create Main && wp --allow-root menu item add-custom Main Reviews https://src-a.test/reviews/ &&
  wp --allow-root option add test_serialized --format=json "{\"u\":\"https://src-a.test/reviews/\"}"; }'
echo seeded
# Leftover backups on source A, for the size report and the "leave them out" question.
# The first group is skipped by the transfer engine itself; the second group is
# what the tool should find and offer to leave out.
docker exec wpm-src-a sh -c 'cd /var/www/html/wp-content && mkdir -p ai1wm-backups updraft backups-dup-lite cache/page backups \
  && head -c 30000000 /dev/urandom > ai1wm-backups/src-a.test-20261101-120000-abc.wpress \
  && head -c 12000000 /dev/urandom > updraft/backup_2026-11-01-1200_Site_abc123-uploads.zip \
  && head -c 8000000 /dev/urandom > backups-dup-lite/20261101_site_abc_archive.zip \
  && head -c 1500000 /dev/urandom > debug.log \
  && head -c 25000000 /dev/urandom > ../old-site-backup.zip \
  && head -c 6000000 /dev/urandom > backups/manual-backup.tar.gz \
  && head -c 4000000 /dev/urandom > ai1wm-backups/renamed-export.zip \
  && head -c 1000000 /dev/urandom > ../db-dump.sql \
  && chown -R www-data:www-data . ../old-site-backup.zip ../db-dump.sql'
