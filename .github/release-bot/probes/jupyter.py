"""Smoke-test a JupyterLab started by the Jupyter app's script.sh on this node.

Usage: python3 jupyter.py PORT HOST PASSWORD
Logs in with the password (as view.html.erb does), checks the API and the Lab
page, then starts a Python kernel and waits for it to become idle.
"""
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

port, host, password = sys.argv[1:4]
base = f"http://localhost:{port}/node/{host}/{port}"
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def req(path, data=None, method=None, as_json=False):
    headers = {"Content-Type": "application/json"} if as_json else {}
    body = json.dumps(data).encode() if as_json else (urllib.parse.urlencode(data).encode() if data else None)
    try:
        r = op.open(urllib.request.Request(base + path, data=body, method=method, headers=headers), timeout=30)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, str(e)


for i in range(300):
    if req("/login")[0]:
        break
    time.sleep(1)
print(f"listening after {i}s")
print("login_page:", req("/login")[0])
print("login:", req("/login", {"password": password})[0])
print("api_status:", req("/api/status")[0])
s, body = req("/lab")
print("lab:", s, "JupyterLab" in body)

s, body = req("/api/kernels", {"name": "python3"}, "POST", as_json=True)
if s != 201:
    print("kernel: start failed", s, body[:120])
    sys.exit(0)
kid = json.loads(body)["id"]
# A kernel reports "idle" only after handling a request, and the REST API
# cannot send one. A restart can: the server waits for the restarted kernel
# to answer a kernel_info request before it replies 200.
time.sleep(5)
s, body = req(f"/api/kernels/{kid}/restart", {}, "POST", as_json=True)
state = "?"
for _ in range(30):
    s2, b2 = req(f"/api/kernels/{kid}")
    state = json.loads(b2).get("execution_state") if s2 == 200 else f"http {s2}"
    if state == "idle":
        break
    time.sleep(2)
print("kernel:", "idle" if s == 200 else f"restart failed ({s}: {body[:120]})", f"(state {state})")
req(f"/api/kernels/{kid}", None, "DELETE")
