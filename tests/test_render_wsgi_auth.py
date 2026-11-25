"""Deployment-only tests for the Render WSGI access gate."""
import base64
import os
import unittest
from unittest import mock


class RenderAuthTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "TRADEMATE_WEB_USER": "operator",
            "TRADEMATE_WEB_PASSWORD": "a-long-demo-only-password-123",
        })
        self.env.start()
        from deploy.auth import BasicAuth
        self.BasicAuth = BasicAuth

    def tearDown(self):
        self.env.stop()

    def call(self, app, path="/", method="GET", headers=None):
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "HTTP_HOST": "trademate.onrender.com",
            "wsgi.input": None,
        }
        for key, value in (headers or {}).items():
            environ[key] = value
        result = {}
        def start_response(status, response_headers):
            result["status"] = status
            result["headers"] = dict(response_headers)
        body = b"".join(app(environ, start_response))
        return result, body

    def test_requires_auth_for_dashboard(self):
        app = self.BasicAuth(lambda env, start: (start("200 OK", []), [b"app"])[1], "operator", "a-long-demo-only-password-123")
        result, body = self.call(app)
        self.assertEqual(result["status"], "401 Unauthorized")
        self.assertIn("WWW-Authenticate", result["headers"])

    def test_accepts_valid_basic_auth(self):
        token = base64.b64encode(b"operator:a-long-demo-only-password-123").decode()
        app = self.BasicAuth(lambda env, start: (start("200 OK", []), [b"app"])[1], "operator", "a-long-demo-only-password-123")
        result, body = self.call(app, headers={"HTTP_AUTHORIZATION": "Basic " + token})
        self.assertEqual(result["status"], "200 OK")
        self.assertEqual(body, b"app")

    def test_health_check_is_public_and_minimal(self):
        app = self.BasicAuth(lambda env, start: (start("500 Error", []), [b"should not run"])[1], "operator", "a-long-demo-only-password-123")
        result, body = self.call(app, path="/healthz")
        self.assertEqual(result["status"], "200 OK")
        self.assertEqual(body, b"ok\n")

    def test_same_origin_write_is_allowed(self):
        token = base64.b64encode(b"operator:a-long-demo-only-password-123").decode()
        app = self.BasicAuth(lambda env, start: (start("200 OK", []), [b"app"])[1], "operator", "a-long-demo-only-password-123")
        result, body = self.call(app, method="POST", headers={
            "HTTP_AUTHORIZATION": "Basic " + token,
            "HTTP_ORIGIN": "https://trademate.onrender.com",
        })
        self.assertEqual(result["status"], "200 OK")

    def test_cross_origin_write_is_refused(self):
        token = base64.b64encode(b"operator:a-long-demo-only-password-123").decode()
        app = self.BasicAuth(lambda env, start: (start("200 OK", []), [b"app"])[1], "operator", "a-long-demo-only-password-123")
        result, body = self.call(app, method="POST", headers={
            "HTTP_AUTHORIZATION": "Basic " + token,
            "HTTP_ORIGIN": "https://evil.example",
        })
        self.assertEqual(result["status"], "403 Forbidden")


if __name__ == "__main__":
    unittest.main()
