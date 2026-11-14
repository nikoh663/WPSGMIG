# Verification

Each scenario was run end to end with `wpmigrate.pyz`. Scenario 2 was run on the final build. The others ran on the build just before the last fix, which only changed what happens on Ctrl+C while Reprint is running. The
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
| 5 | Existing site, "remove and replace" | `05-remove.txt` | Pass. Removal needs the domain typed in. It reuses the old site's database details, removes the non-WordPress file (google1234.html) when told to (backup is the default), and the new site loads. |
| 6 | Existing site, "keep", subdomain folder created while waiting | `06-keep.txt` | Pass. The old site moves to `old.dest.test/public_html` and boots under WP-CLI with home `https://old.dest.test`, `blog_public` 0, and its links rewritten. The new site loads at https://dest.test. Separate databases (site_old and site_new). |
| 7 | "Keep", subdomain folder never created | `07-keep-no-subdomain.txt` | Pass. It offers check again, another path, or cancel. Nothing is moved, the old site is still served, and the new database stays empty. Exit 14. |
| 8 | Stopped at stage 7, continued later with credentials | `08a-stop-at-database.txt`, `08b-continue-with-database.txt` | Pass. Ctrl+C at "Database host". The next run skips straight to the database questions (the folder answer was kept) and finishes. |
| 9 | Subfolder chosen at the folder question | `09-subfolder.txt` | Pass. `blog` creates the folder and suggests https://dest.test/blog. The database address is rewritten, the `.htaccess` rules are adjusted, and the home page and a page load under /blog/. |

Extra runs for the layout decision: `10-layout-link.txt` (symlinks: 403,
removed from the tool) and `11-layout-copy.txt` (copy: works, no symlinks).

Unit tests: 54 pass on Python 3.13 and on Python 3.10.

## Bugs found and fixed during testing

- Scenario 2 failed once in the final pass: the resume run said "Another Reprint process is using the state directory". Ctrl+C had left Reprint running in the background for a while. The tool now stops Reprint and waits for it to exit before it quits, and the transcript checks that no Reprint process is left.
- Scenario 9 first failed: inner pages answered 404 under /blog/. The copied
`.htaccess` still had `RewriteBase /`. The tool now rewrites the standard
WordPress block to match the new address's path, and a unit test covers it.

## Not tested

- Downloading `reprint.phar` and the plugin from wordpress.org. That site is
  blocked from the test machine, so a phar built from the v0.11.8 tag was passed
  with `--reprint-phar`. The test sites installed the plugin from a local copy.
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
- Very large sites. The test site is 163 MB and 3,839 files.
