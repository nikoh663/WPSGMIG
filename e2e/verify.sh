#!/usr/bin/env bash
# Independent checks after a scenario (not part of the tool).
export no_proxy="$no_proxy,.test" SSL_CERT_FILE=/etc/ssl/wpm-combined.pem
W="runuser -u wpuser -- php /opt/wpm-ref/wp-cli.phar"
check_site() { # url marker
  local body code
  code=$(curl -s -o /tmp/verify-body -w '%{http_code}' "$1")
  if grep -q "$2" /tmp/verify-body; then echo "OK   $1 -> HTTP $code, contains '$2'"; else echo "FAIL $1 -> HTTP $code, missing '$2'"; fi
}
for arg in "$@"; do
  case "$arg" in
    main) check_site https://dest.test/ SRC-A-7731; check_site https://dest.test/sample-page/ "Sample Page" ;;
    main-b) check_site https://dest.test/ SRC-B-7731 ;;
    sub) check_site https://dest.test/blog/ SRC-A-7731; check_site https://dest.test/blog/sample-page/ "Sample Page" ;;
    old)
      echo "old site home option (WP-CLI, full boot): $($W --path=/home/wpuser/www/old.dest.test/public_html option get home)"
      echo "old site blog_public: $($W --path=/home/wpuser/www/old.dest.test/public_html option get blog_public)"
      check_site https://old.dest.test/ OLD-DEST-MARKER-5512
      echo "old site DB: $(grep DB_NAME /home/wpuser/www/old.dest.test/public_html/wp-config.php)"
      echo "new site DB: $(grep DB_NAME /home/wpuser/www/dest.test/public_html/wp-config.php)"
      echo "old site post links: $($W --path=/home/wpuser/www/old.dest.test/public_html post list --post_type=post --field=post_content | grep -o 'https://[a-z.]*/old-page/')" ;;
    tables)
      docker exec wpm-dest-db mariadb -uroot -prootpw -N -e "SELECT table_schema, COUNT(*) FROM information_schema.tables WHERE table_schema IN ('site_new','site_old','site_two') GROUP BY table_schema;"
      docker exec wpm-dest-db mariadb -uroot -prootpw -N -e "SELECT 'site_new home:', option_value FROM site_new.wp_options WHERE option_name='home'; SELECT 'site_old home:', option_value FROM site_old.wp_options WHERE option_name='home';" 2>/dev/null ;;
    css) echo "css: $(curl -s https://dest.test/wp-content/themes/marker-theme/style.css | grep url)" ;;
  esac
done
