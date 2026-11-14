"""Environment for running the tool as the unprivileged test user."""
import json, os, sys
env = {
    "HOME": "/home/wpuser", "USER": "wpuser", "LOGNAME": "wpuser", "PATH": "/usr/local/bin:/usr/bin:/bin",
    "TERM": "xterm", "LANG": "C.UTF-8",
    "SSL_CERT_FILE": "/etc/ssl/wpm-combined.pem",
    "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", ""), "https_proxy": os.environ.get("https_proxy", os.environ.get("HTTPS_PROXY", "")),
    "NO_PROXY": os.environ.get("NO_PROXY", "") + ",.test", "no_proxy": os.environ.get("no_proxy", "") + ",.test",
}
print(json.dumps(env))
