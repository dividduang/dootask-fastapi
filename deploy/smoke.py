"""Real HTTP deployment smoke checks; creates synthetic fixtures only on explicit --fresh.

Run inside app: python -m deploy.smoke --fresh
Repeat/read after container recreation: python -m deploy.smoke
Private credentials and fixture ids persist in /data/deployment-state.json (0600).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from websockets.sync.client import connect

BASE = "http://127.0.0.1:18080"
STATE = Path("/data/deployment-state.json")


def request(path, data=None, token="", *, raw=False, multipart=None):
    headers = {"Content-Type": "application/json", "version": "1.9.66"}
    if token:
        headers["dootask-token"] = token
    body = json.dumps(data or {}).encode()
    if multipart:
        body, headers["Content-Type"] = multipart
    try:
        response = urlopen(Request(BASE + "/api/" + path, data=body, headers=headers), timeout=30)
    except HTTPError as error:
        response = error
    with response:
        payload = response.read()
        return payload if raw else json.loads(payload)


def ok(path, data=None, token=""):
    result = request(path, data, token)
    if result.get("ret") != 1:
        raise RuntimeError(f"{path} failed: ret={result.get('ret')} msg={result.get('msg')}")
    return result["data"]


def save(state):
    os.umask(0o077)
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temp.chmod(0o600)
    temp.replace(STATE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args()
    if args.fresh:
        if STATE.exists():
            raise SystemExit("Refusing existing deployment state; use readback mode")
        from backend.config import Settings
        from backend.db import make_engine
        from sqlalchemy import text
        engine = make_engine(Settings.from_env())
        try:
            with engine.connect() as connection:
                if connection.execute(text("SELECT COUNT(*) FROM users")).scalar_one():
                    raise SystemExit("Fresh validation requires an empty users table")
        finally:
            engine.dispose()
        state = {"admin": {"email": "admin@example.test", "password": "Dt7!" + secrets.token_urlsafe(15)},
                 "outsider": {"email": "smoke@example.test", "password": "Dt7!" + secrets.token_urlsafe(15)}}
        save(state)  # Keep credentials even if a later check fails.
        for role in ("admin", "outsider"):
            user = ok("users/login", {"type": "reg", **state[role]})
            assert ("admin" in user["identity"]) == (role == "admin")
        print("PASS registration: one administrator and one ordinary synthetic user")
    else:
        state = json.loads(STATE.read_text())
    admin = ok("users/login", state["admin"])
    outsider = ok("users/login", state["outsider"])
    token = admin["token"]
    assert ok("users/info", token=token)["userid"] == admin["userid"]
    for path in ("/health", "/login"):
        with urlopen(BASE + path, timeout=10) as response:
            assert response.status == 200
    manifest = json.loads(Path("/app/frontend/public/manifest.json").read_text())
    entry = manifest["resources/assets/js/app.js"]
    for asset in (entry["file"], entry["css"][0]):
        with urlopen(BASE + "/" + asset, timeout=10) as response:
            assert response.status == 200 and response.read(1)
    print("PASS login, user identity, page and built frontend assets")
    if args.fresh:
        project = ok("project/add", {"name": "Docker deployment verification", "columns": "Backlog,Done"}, token)
        column = ok("project/column/lists", {"project_id": project["id"]}, token)["data"][0]
        task = ok("project/task/add", {"project_id": project["id"], "column_id": column["id"],
                                      "name": "Persistent Docker check", "owner": [admin["userid"]]}, token)
        payload = "DooTask Docker persistence: 验证成功\n".encode()
        boundary = "deploy-" + secrets.token_hex(12)
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="docker-smoke.txt"\r\n'
                'Content-Type: text/plain\r\n\r\n').encode() + payload + f"\r\n--{boundary}--\r\n".encode()
        upload = request("file/content/upload", token=token, multipart=(body, "multipart/form-data; boundary=" + boundary))
        assert upload["ret"] == 1
        state.update(project_id=project["id"], task_id=task["id"], file_id=upload["data"][-1]["id"],
                     file_sha256=hashlib.sha256(payload).hexdigest())
        save(state)
    assert ok("project/one", {"project_id": state["project_id"]}, token)["id"] == state["project_id"]
    assert ok("project/task/one", {"task_id": state["task_id"]}, token)["name"] == "Persistent Docker check"
    payload = request("file/content", {"id": state["file_id"], "down": "yes"}, token, raw=True)
    assert hashlib.sha256(payload).hexdigest() == state["file_sha256"]
    assert request("project/one", {"project_id": state["project_id"]}, outsider["token"])["ret"] != 1
    assert request("file/one", {"id": state["file_id"]}, outsider["token"])["ret"] != 1
    print("PASS persisted project, task, uploaded bytes and outsider access rejection")
    with connect("ws://127.0.0.1:18080/ws?action=web&token=" + quote(token), open_timeout=15) as socket:
        opened = False
        for _ in range(10):
            frame = json.loads(socket.recv(timeout=15))
            if frame.get("type") == "open":
                assert frame["data"]["ud"] == admin["userid"]
                opened = True
                break
        assert opened
    print("PASS authenticated WebSocket identity")
    ok("users/logout", token=token)
    assert request("users/info", token=token)["ret"] != 1
    ok("users/logout", token=outsider["token"])
    print("PASS logout and session revocation")
    print("Deployment validation passed; credentials are private in /data/deployment-state.json")


if __name__ == "__main__":
    main()
