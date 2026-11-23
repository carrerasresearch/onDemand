"""Smoke-test an RStudio Server started by the app's script.sh on this node.

Usage: python3 probe.py PORT USER
Signs in the way view.html.erb does (auth-none), loads the IDE page with a
browser user agent (RStudio rejects unknown browsers), then waits for rserver
to spawn an rsession and prints its command line (incl. --r-libs-user).
"""
import http.cookiejar
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

port, user = sys.argv[1], sys.argv[2]
base = f"http://localhost:{port}"
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
op.addheaders = [("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/142.0 Safari/537.36")]


def get(path, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    try:
        r = op.open(base + path, data=body, timeout=20)
        return r.status, r.geturl(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, path, ""


for i in range(180):
    try:
        urllib.request.urlopen(base + "/auth-sign-in", timeout=2)
        break
    except urllib.error.HTTPError:
        break
    except Exception:
        time.sleep(1)
print(f"listening after {i}s")
print("sign_in:", get("/auth-sign-in")[0])
s, url, _ = get("/auth-do-sign-in", {"username": user, "password": "x", "staySignedIn": "1", "appUri": ""})
print("do_sign_in:", s, "->", url)
s, url, body = get("/")
t = re.search(r"<title>([^<]*)", body)
print("ide:", s, url, "title:", t.group(1) if t else None)

# rserver starts the R session when the IDE client initialises. Calling
# client_init without the browser's CSRF token fails, but still triggers it.
rserver = set(subprocess.run(["pgrep", "-u", user, "-f", f"rserver --www-port {port}"],
                             capture_output=True, text=True).stdout.split())


def descends_from_rserver(pid):
    while pid not in ("0", "1"):
        if pid in rserver:
            return True
        try:
            pid = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1]
        except OSError:
            return False
    return False


init = urllib.request.Request(base + "/rpc/client_init", data=b'{"method":"client_init","params":[]}',
                              headers={"Content-Type": "application/json"})
for _ in range(60):
    try:
        op.open(init, timeout=10).read()
    except Exception:
        pass
    for pid in subprocess.run(["pgrep", "-u", user, "rsession"], capture_output=True, text=True).stdout.split():
        if descends_from_rserver(pid):
            args = open(f"/proc/{pid}/cmdline").read().replace("\0", " ").strip()
            print("rsession:", args[:200])
            sys.exit(0)
    time.sleep(2)
print("rsession: not started")
