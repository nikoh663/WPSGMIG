# Verification

Each scenario was run end to end with `wpmigrate.pyz`. All scenarios were run again on version 1.3.4. The
tool ran as an unprivileged user (`wpuser`) inside a pseudo-terminal, typing
answers the way a person would. The full output of each run is in
`e2e/results/`, followed by independent checks made with curl, WP-CLI and SQL.

**Test setup** (Docker, see `e2e/env/`):

- Two source WordPress sites: `src-a.test` (normal) and `src-b.test` (with
  `DISALLOW_FILE_MODS`).
- A destination web server that serves `/home/wpuser/www/<domain>/public_html`
  over HTTPS, with Apache `SymLinksIfOwnerMatch` and running as another user.
- A separate database server.

| # | Scenario from the spec | Output | Result |
|---|---|---|---|
| 1 | Clean run into an empty folder | `01-clean.txt` | Pass. The site and a page load at https://dest.test with the right content and rewritten asset URLs. Neither password appears in the private folder or the site folder (outside `wp-config.php`). |
| 2 | Interrupted during download, then resumed | `02a-interrupt.txt`, `02b-resume.txt` | Pass. Ctrl+C partway through the file download gives exit 14. The next run asks "Continue it?", carries on from where the download stopped, and finishes. The site loads. |
| 2b | Dropped SSH session | `02c-ssh-drop.txt`, `02d-after-ssh-drop.txt` | Pass. The terminal is closed during the download. The tool finishes the download in the background and stops cleanly at the first question it cannot ask. The next run continues and finishes. |
| 3 | Wrong password | `03-wrong-password.txt` | Pass. 3 tries (3 login POSTs in the log), then it stops with exit 10. The typed passwords are not in any file. |
| 4 | `DISALLOW_FILE_MODS`, manual fallback | `04-file-mods-manual.txt` | Pass. The install is refused, so the tool prints the Migration Token and click-by-click steps. The first Enter correctly reports that the site is not authorized yet. After the plugin is installed and the token enrolled, it continues and the site loads. |
| 5 | Existing site, "remove and replace" | `05-remove.txt` | Pass. Removal needs the domain typed in. It reuses the old site's database details, removes the non-WordPress file (google1234.html) when told to (backup is the default; it can also be moved to another folder), and the new site loads. |
| 6 | Existing site, "keep", subdomain folder created while waiting | `06-keep.txt` | Pass. The old site moves to `old.dest.test/public_html` and boots under WP-CLI with home `https://old.dest.test`, `blog_public` 0, and its links rewritten. The new site loads at https://dest.test. Separate databases (site_old and site_new). |
| 7 | "Keep", subdomain folder never created | `07-keep-no-subdomain.txt` | Pass. It offers check again, another path, or cancel. Nothing is moved, the old site is still served, and the new database stays empty. Exit 14. |
| 8 | Stopped at stage 7, continued later with credentials | `08a-stop-at-database.txt`, `08b-continue-with-database.txt` | Pass. Ctrl+C at "Database host". The next run skips straight to the database questions (the folder answer was kept) and finishes. |
| 9 | Subfolder chosen at the folder question | `09-subfolder.txt` | Pass. `blog` creates the folder and suggests https://dest.test/blog. The database address is rewritten, the `.htaccess` rules are adjusted, and the home page and a page load under /blog/. |
| 10 | Started inside a subfolder (`<domain>/public_html/new`), Enter pressed for every default | `12-started-in-subfolder.txt` | Pass. The tool suggests https://dest.test/new as the address. The database address and the `.htaccess` rules use /new, and the home page and a page load under /new/. Added after a real run kept the old address. |
| 11 | Same site copied again into the same folder after a finished copy | `13-copy-again.txt` | Pass. The tool asks whether to copy again from the start, then treats the earlier copy as an existing site: remove needs the domain typed, and its database details are offered again. The site loads under /new/. |
| 12 | Size report and leftover backups | `01-clean.txt`, `14-keep-backups.txt` | Pass. The test site has backups from All-in-One WP Migration, UpdraftPlus and Duplicator (never copied), plus a 25 MB zip, a backup folder, a renamed export and a database dump. The tool shows files and database sizes, says about 55 MB of plugin backups, cache and logs stay behind, numbers the four leftovers (34.3 MB) and asks. Enter (the default) copies none of them. Picking "let me pick" and then `1` copies only the 25 MB zip and none of the other three. |
| 13 | Existing site in a subfolder (`public_html/new`), "keep" | `15-keep-in-subfolder.txt` | Pass. The old site moves to `<base>/old.dest.test/public_html` (next to the domain's folder, not inside it), boots under WP-CLI at https://old.dest.test with its `.htaccess` reset to `/`, and the copy loads at https://dest.test/new. Added after a real run offered a folder nested inside the domain's folder. |
| 14 | Existing site, "keep", the new subdomain folder holds only host files (`.well-known`, `Default.html`) | `16-keep-harmless-files.txt` | Pass. The tool does not ask about them, says it left them as they are, and moves the old site in next to them. Both are still there afterwards, and both sites load. |
| 15 | Site copied into a subfolder of its own address (`https://src-a.test` to `https://src-a.test/new`) | `17-same-domain-subfolder.txt` | Pass. Links in post content, the menu, post meta and serialized options all point to `https://src-a.test/new/...`, with no `/new/new` and no temporary address left anywhere, including the guid and a table without a primary key (plain and serialized values). The address step took about 3 seconds with WP-CLI. Added after a real run gave `/new/new/` links. The test web server does not answer for src-a.test, so the online check reports 404 there. |
| 16 | Existing site, "keep", other files moved along with the old site | `18-keep-move-other-files.txt` | Pass. Choosing "Move them to the old site's folder" puts google1234.html in `old.dest.test/public_html` and not in the backup folder. Both sites load. |

Extra runs for the layout decision: `10-layout-link.txt` (symlinks: 403,
removed from the tool) and `11-layout-copy.txt` (copy: works, no symlinks).

Unit tests: 59 pass on Python 3.13 and on Python 3.10.

## Bugs found and fixed during testing

- A real run into `/new` on the same domain gave links such as `/new/new/reviews/`. It is caused by the transfer engine (see REPRINT-DIFFERENCES.md, item 5) and only shows when the new address contains the old one, so the earlier tests, which copied to another domain, could not see it. Scenario 17 and unit tests now cover it.
- Scenario 2 failed once in the final pass: the resume run said "Another Reprint process is using the state directory". Ctrl+C had left Reprint running in the background for a while. The tool now stops Reprint and waits for it to exit before it quits, and the transcript checks that no Reprint process is left.
- Scenario 9 first failed: inner pages answered 404 under /blog/. The copied
`.htaccess` still had `RewriteBase /`. The tool now rewrites the standard
WordPress block to match the new address's path, and a unit test covers it.

## Not tested

- Downloading `reprint.phar` and the plugin from wordpress.org is blocked from
  the test machine, so the tests pass a phar built from the v0.11.8 tag with
  `--reprint-phar`. Both downloads did work in a real run on shared hosting.
- Real CAPTCHA, two-factor, Cloudflare or other bot-check pages, and security
  plugins that rename wp-login.php. Only unit tests with sample pages cover
  these: the tool recognises them and switches to the manual steps.
- A source site that uses a connection token instead of key enrollment. The
  code path exists, but the test sites accepted keys.
- Real shared hosting. Only a Docker imitation was used: separate users, a
  0700 home folder, `SymLinksIfOwnerMatch`, and no root.
- Old PHP versions on the destination. Only PHP 8.3 on the command line and
  PHP 8.2 on the web were used, so the PHP-mismatch warning was seen only by
  reading the code path.
- The size of plugin backups comes from WordPress's Site Health numbers. On a
  very large site WordPress may give up on that count, and then the tool only
  names the backup plugins it can see. Plugins whose backup folder holds only
  the archives themselves (no `index.php` next to them) are not named.
- Speed on real hosting. On the test machine a 386 MB site (197 MB of files, 189 MB of database) copies in about 36 seconds, so the tool itself adds little; on real hosting the time depends on how fast the old site can send its files and how fast the new database imports. The summary now shows how long each step took.
- Very large sites. The test site is about 390 MB: 197 MB in 3,843 files and a 189 MB database.
