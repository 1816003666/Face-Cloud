import urllib.request, json
BASE="http://127.0.0.1:8003"
def post(url, data, tok=None):
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type","application/json")
    if tok: req.add_header("Authorization","Bearer "+tok)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]
def get(url, tok):
    req = urllib.request.Request(url)
    req.add_header("Authorization","Bearer "+tok)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300]
sc, body = post(BASE+"/api/auth/login", {"username":"admin","password":"Admin@123456"})
print("login:", sc, body[:200])
tok=""
try:
    j=json.loads(body)
    tok=j.get("access_token") or j.get("token") or (j.get("data") or {}).get("token") or ""
except Exception as e:
    print("parse:", e)
print("token len:", len(tok))
if tok:
    sc, body = post(BASE+"/api/devices/2/control/tap", {"x":500,"y":500}, tok)
    print("control tap dev2:", sc, body[:300])
    sc, body = get(BASE+"/api/devices", tok)
    print("devices:", sc)
    try:
        jj=json.loads(body); data=jj.get("data", jj) if isinstance(jj,dict) else jj
        devs=data if isinstance(data,list) else (data.get("items",[]) if isinstance(data,dict) else [])
        for dd in (devs or [])[:8]:
            print("  ", dd.get("id"), dd.get("name"), dd.get("serial"), dd.get("status"))
    except Exception as e:
        print("parse devices:", e, body[:200])
