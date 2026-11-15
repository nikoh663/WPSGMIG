# Where Reprint differed from its docs

These were checked against Reprint client v0.11.8, built from the `v0.11.8` tag,
and the handbook in that same tag (`docs/handbook/`). Every command, flag and
field name the tool uses was taken from that source and from each command's
`--help`.

1. **`apply-runtime --runtime=none` is rejected.**
   - What the docs say: `commands.md` lists `none` as a valid value of
     `--runtime` for both `pull` and `apply-runtime`.
   - What happened: `apply-runtime` fails with "Unknown runtime: none. Valid
     runtimes: php-builtin, nginx-fpm, playground-cli". The applier in
     `target-runtime/functions.php` removes `none` from the list. Only `pull`
     accepts it.
   - Workaround: the tool runs `--runtime=php-builtin` into a private folder,
     then deletes the generated `runtime.php` and `start.sh`. They are not used,
     and they contain the database password.

2. **The destination database password is stored in plain text.**
   - What the docs say: they never say so, and they never warn that the state
     folder holds a password. They only say to keep it out of the web root.
     `runtime.md` hints at it ("otherwise you inherit `db-apply`'s ... password").
   - What happened: `db-apply` saves `--target-pass` as `target_pass` in its
     state (`pull/state.json`), and the php-builtin `runtime.php` embeds it too.
   - Workaround: the state folder is 0700. After a successful run the tool
     deletes Reprint's state and keeps only its audit log. If the run stops
     before that, the summary warns about it.

3. **Secrets can only be passed on the command line.**
   - What the docs say: they don't flag this. Every example passes the secret
     in argv.
   - What happened: `--target-pass` and `--secret` (the connection token) have
     no environment variable, file or stdin form. In argv they show in the
     process list. `MYSQL_PASSWORD` exists, but only for `db-pull
     --sql-output=mysql`.
   - Workaround: the tool starts PHP with a short fixed launcher (`php -r`).
     The launcher reads the secret arguments as JSON from stdin and adds them to
     Reprint's arguments inside the process, so they never appear in argv.

4. **Re-running a finished `db-apply` is an error.**
   - What the docs say: they say "run the same command" to continue. For a
     finished `files-pull`, `file-transfers.md` documents that `--abort` is
     needed first, but nothing like that is said for `db-apply`.
   - What happened: running `db-apply` again after it completed fails with
     "already completed. Use --abort".
   - Workaround: the tool records in its own state that the import is done and
     never repeats it.

5. **Moving a site into a subfolder of its own domain doubles some links.**
   - What the docs say: `--new-site-url` (and `--rewrite-url FROM TO`)
     replaces the old address with the new one.
   - What happened: with `--new-site-url=https://example.com/new` for a site at
     `https://example.com`, links inside post content came out as
     `https://example.com/new/new/page/`. Menu links, options and post meta
     were fine. `StructuredDataUrlRewriter::rewrite_urls()` rewrites each URL
     found in block markup with `set_url()`, then runs
     `replace_url_bases_in_current_token()` over the same token. When the new
     address still contains the old one, the second pass matches again.
     Reproduced with `db-rewrite-urls` on a test table.
   - Workaround: when the new address contains the old one, the tool rewrites
     in two steps: old address to `https://wpmigrate-new-address.invalid`
     during `db-apply`, then that to the new address with `db-rewrite-urls`.
     Neither step's target contains its source.

## Not Reprint, but worth knowing

- The `reprint.phar` download from wordpress.org could not be tested, because
  wordpress.org is not reachable from the test machine. The tests used a phar
  built from the v0.11.8 tag with Box 4.7.0 (`--reprint-phar`).
- The server plugin used on the test source sites was built from the same tag,
  without the PHP 5.6 compatibility step of the official build. The sites ran
  PHP 8.2, so this should not matter.
