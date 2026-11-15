#!/usr/bin/env bash
# Scenario 17: copy https://src-a.test into a subfolder of its own address (https://src-a.test/new).
cd /home/user/wpm/e2e && ./reset.sh >/dev/null && /usr/sbin/runuser -u wpuser -- mkdir -p /home/wpuser/www/dest.test/public_html/new
ENVS=$(python3 -c "import json,subprocess,shlex;d=json.loads(subprocess.check_output(['python3','envjson.py']));print(' '.join(shlex.quote(f'{k}={v}') for k,v in d.items()))")
OUT=/home/user/wpm/e2e/results/17-same-domain-subfolder.txt
{ echo "\$ python3 wpmigrate.pyz --non-interactive --source https://src-a.test --user admin --new-url https://src-a.test/new --db-host 172.30.0.12 --db-name site_new --db-user site_new --replace-tables --cleanup-source yes"
  echo "(run in /home/wpuser/www/dest.test/public_html/new; the test web server does not answer for src-a.test, so the online check reports 404)"; echo; } > $OUT
cd /home/wpuser/www/dest.test/public_html/new
eval /usr/sbin/runuser -u wpuser -- /usr/bin/env -i $ENVS "WPMIGRATE_ADMIN_PASSWORD='Corr3ct-Horse!'" WPMIGRATE_DB_PASSWORD=New-Db-Pass-1 NO_COLOR=1 python3 /opt/wpmigrate/wpmigrate.pyz --reprint-phar=/opt/wpmigrate/reprint.phar --non-interactive --source https://src-a.test --user admin --new-url https://src-a.test/new --db-host 172.30.0.12 --db-name site_new --db-user site_new --replace-tables --cleanup-source yes >> $OUT 2>&1
echo "[exit code: $?]" >> $OUT
W="/usr/sbin/runuser -u wpuser -- php /opt/wpm-ref/wp-cli.phar"
{ echo "--- checks (WP-CLI on the copy)"
  $W eval 'global $wpdb;
    echo "home: ", get_option("home"), "\n";
    echo "Reviews page: ", $wpdb->get_var("select post_content from {$wpdb->posts} where post_name=\"reviews\" and post_type=\"page\""), "\n";
    echo "menu link: ", $wpdb->get_var("select meta_value from {$wpdb->postmeta} where meta_key=\"_menu_item_url\" and meta_value<>\"\" limit 1"), "\n";
    echo "rows with /new/new: ", $wpdb->get_var("select count(*) from {$wpdb->posts} where post_content like \"%/new/new%\""), "\n";
    echo "rows with the temporary address: ", $wpdb->get_var("select count(*) from {$wpdb->posts} where post_content like \"%wpmigrate-new-address%\""), "\n";'
} >> $OUT 2>&1
