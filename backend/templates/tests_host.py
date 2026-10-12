"""Run manually: python -m unittest backend.templates.tests_host (not executed here)."""

import asyncio
import json
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from jinja2 import UndefinedError
from markupsafe import Markup
from starlette.exceptions import HTTPException

from backend.host import install_host, render_template, verify_csrf


class HostContract(unittest.TestCase):
    def setUp(self):
        self.frontend = Path(__file__).resolve().parents[2] / "frontend"
        self.settings = SimpleNamespace(
            frontend_root=self.frontend, system_alias="Host contract", login_logo="images/logo.png",
            debug=False, version="contract-check", locale="en", csrf_secret="unit-check-secret-" * 3,
        )
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        install_host(self.app, self.settings)
        self.endpoint = self.app.routes[-1].endpoint

    def request(self, path="/", cookie="", token=""):
        return Request({
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
            "server": ("example.test", 80), "client": ("127.0.0.1", 1), "root_path": "",
            "path": path, "query_string": b"", "app": self.app,
            "headers": [(b"host", b"example.test"), (b"cookie", cookie.encode()),
                        (b"x-csrf-token", token.encode())],
        })

    def page(self, path):
        return asyncio.run(self.endpoint(self.request(path), path.lstrip("/")))

    def test_html_resources_routing_and_exclusions(self):
        manifest = json.loads((self.frontend / "public" / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest["resources/assets/js/app.js"]
        for path in (
            "/", "/pro", "/login", "/token", "/preload", "/manage", "/meeting", "/meeting/12/key",
            "/manage/dashboard", "/manage/calendar", "/manage/messenger/open",
            "/manage/setting", "/manage/setting/system-update", "/manage/setting/license",
            "/manage/project/invite", "/manage/project/invite/key", "/manage/project/12",
            "/manage/file", "/manage/file/12/34", "/manage/application",
            "/single/file/msg/12", "/single/file/task/12", "/single/file/12",
            "/single/task/content/12", "/single/task/12", "/single/dialog/12",
            "/single/apps/example", "/single/valid/email", "/single/report/edit/12",
            "/single/report/detail/12/",
        ):
            with self.subTest(path=path):
                response = self.page(path)
                self.assertEqual(response.status_code, 200)
                html = response.body.decode()
                self.assertIn('id="app" data-preload="init"', html)
                self.assertIn('//example.test/' + entry["file"], html)
                self.assertIn('//example.test/' + entry["css"][0], html)
                self.assertIn('debug: "no"', html)
        for path in (
            "/missing.js", "/js/missing.js", "/index.php", "/INDEX.PHP", "/index.php.bak",
            "/web.config", "/.env", "/manifest.json", "/resources/assets/js/app.js",
            "/js/../index.php", "/js/example.js:secret", "/js/%2e%2e/index.php", "/css/fonts/",
            "/api/unknown/endpoint", "/ws", "/dav", "/avatar", "/desktop/publish/latest",
            "/online/preview", "/drawio/iconsearch", "/crontab", "/manage/unknown",
            "/meeting/1/2/3", "/single/task", "/does-not-exist",
        ):
            with self.subTest(path=path), self.assertRaises(HTTPException) as raised:
                self.page(path)
            self.assertEqual(raised.exception.status_code, 404)
        css = self.page("/" + entry["css"][0])
        self.assertEqual(css.status_code, 200)
        self.assertTrue(css.headers["content-type"].startswith("text/css"))
        self.assertEqual(self.page("/health").body, b"ok")
        version = self.page("/version")
        self.assertEqual(version.status_code, 301)
        self.assertEqual(version.headers["location"], "http://example.test/api/system/version")

    def test_signed_csrf_and_safe_templates(self):
        response = self.page("/")
        cookie = SimpleCookie(response.headers["set-cookie"])["dootask_csrf"].value
        token = cookie.partition(".")[0]
        request = self.request(cookie="dootask_csrf=" + cookie, token=token)
        self.assertTrue(verify_csrf(request, self.settings.csrf_secret))
        self.assertFalse(verify_csrf(request, self.settings.csrf_secret, "0" * 64))
        tampered = ("0" if cookie[0] != "0" else "1") + cookie[1:]
        self.assertFalse(verify_csrf(self.request(cookie="dootask_csrf=" + tampered, token=token), self.settings.csrf_secret))
        self.assertIn(token.encode(), response.body)
        self.settings.system_alias = '</script><script>alert("x")</script>'
        html = self.page("/").body.decode()
        self.assertNotIn(self.settings.system_alias, html)
        self.assertIn(r"\u003c/script\u003e", html)
        report = render_template("report", {"labels": ["<label>"], "datas": [["<b>unsafe</b>", Markup("<b>trusted</b>")]]})
        self.assertIn(b"&lt;b&gt;unsafe&lt;/b&gt;", report.body)
        self.assertIn(b"<b>trusted</b>", report.body)
        with self.assertRaises(UndefinedError):
            render_template("report", {"labels": []})
        with self.assertRaises(ValueError):
            render_template("download", {
                "csrf_token": token, "name": "file", "system_alias": "Host contract",
                "url": "javascript:alert(1)", "button": "Download", "size": "1 B",
            })


if __name__ == "__main__":
    unittest.main()
