"""使用合成数据和本地模拟协议检查边界，不读取真实凭据或访问网络。"""
import base64
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch, MagicMock

import collect as c


def token(claims):
    return "test." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".test"


def valid_auth():
    return {"tokens": {"access_token": token({"exp": time.time() + 3600}), "account_id": "test-account"}}


def raw_window(seconds=604800, percent=46):
    return {"used_percent": percent, "limit_window_seconds": seconds, "reset_at": 1789207556}


def usage():
    return {"rate_limit": {"primary_window": raw_window(), "secondary_window": None}}


class AuthTests(unittest.TestCase):
    def test_expiry_boundary(self):
        for delta, expected in ((299, True), (300, True), (301, False)):
            self.assertEqual(c.needs_refresh({"tokens": {"access_token": token({"exp": 1000 + delta})}}, 1000), expected)

    def test_last_refresh_fallback(self):
        self.assertFalse(c.needs_refresh({"last_refresh": "2026-01-01T00:00:00Z"}, 1767225600 + 7 * 86400))
        self.assertTrue(c.needs_refresh({"last_refresh": "2026-01-01T00:00:00Z"}, 1767225600 + 8 * 86400))
        self.assertTrue(c.needs_refresh({}, 0))
        self.assertTrue(c.needs_refresh({"last_refresh": "bad"}, 0))

    def test_account_claim(self):
        access = token({"https://api.openai.com/auth": {"chatgpt_account_id": "test-account"}})
        self.assertEqual(c.credentials({"tokens": {"access_token": access}}), (access, "test-account"))

    def test_invalid_credentials(self):
        for tokens in ({}, {"access_token": "bad"}, {"access_token": "bad\n", "account_id": "x"}):
            with self.assertRaises(c.QueryError):
                c.credentials({"tokens": tokens})
        self.assertEqual(c.jwt_claims("not-a-jwt"), {})


class WindowTests(unittest.TestCase):
    def test_weekly_only(self):
        result = c.normalize(usage())
        self.assertEqual(result["windows"][0]["label"], "周额度")
        self.assertEqual(result["windows"][0]["used"], 46)
        self.assertIsNone(result["resetCredits"])

    def test_reversed_and_extra_windows(self):
        data = usage()
        data["rate_limit"]["secondary_window"] = raw_window(18000, 0)
        data["additional_rate_limits"] = [{"limit_name": "Spark", "rate_limit": {"primary_window": raw_window(18000, 20)}}]
        result = c.normalize(data)["windows"]
        self.assertEqual([w["used"] for w in result], [0, 46, 20])
        self.assertEqual(result[2]["group"], "Spark")

    def test_missing_is_not_zero(self):
        for value in (None, True, "0", float("nan"), float("inf")):
            with self.assertRaises(c.QueryError):
                c.normalize({"rate_limit": {"primary_window": raw_window(percent=value)}})

    def test_rpc_prefers_multiple_buckets(self):
        data = {"rateLimits": {"primary": {"usedPercent": 99}}, "rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 12, "windowDurationMins": 10080}},
            "spark": {"primary": {"usedPercent": 0, "windowDurationMins": 300}, "limitName": "Spark"}},
            "rateLimitResetCredits": {"availableCount": 2}}
        result = c.normalize(data, True)
        self.assertEqual([w["used"] for w in result["windows"]], [12, 0])
        self.assertEqual(result["resetCredits"], 2)

    def test_output_excludes_identity(self):
        data = usage() | {"email": "not-for-ui", "account_id": "private-account"}
        result = json.dumps(c.normalize(data))
        self.assertNotIn("not-for-ui", result)
        self.assertNotIn("private-account", result)

    def test_invalid_time_and_count(self):
        self.assertEqual(c.iso_time(1e100), "")
        self.assertIsNone(c.reset_count({"available_count": True}))
        self.assertEqual(c.reset_count({"available_count": 0}), 0)


class TransportTests(unittest.TestCase):
    def test_fixed_endpoint_headers_and_timeout(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(c.urllib.request, "build_opener", return_value=opener):
            c.http_json("usage", "synthetic-token", "test-account", 30)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, c.API_ROOT + "usage")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 30)
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIsNone(c.NoRedirect().redirect_request(request, None, 302, "", {}, "https://example.test"))

    def test_http_errors_do_not_disclose_body(self):
        for code in (401, 403, 429, 500, 302):
            error = urllib.error.HTTPError(c.API_ROOT, code, "private-text", {}, io.BytesIO(b"private-text"))
            with patch.object(c.urllib.request, "build_opener") as build:
                build.return_value.open.side_effect = error
                with self.assertRaises(c.QueryError) as result:
                    c.http_json("usage", "test", "test", 1)
                self.assertNotIn("private-text", str(result.exception))

    def test_credits_failure_preserves_usage(self):
        with patch.object(c, "http_json", side_effect=[usage(), c.QueryError("unavailable")]), patch.object(c, "cli_limits") as cli:
            self.assertTrue(c.query(Path("/tmp"), valid_auth())["ok"])
            cli.assert_not_called()

    def test_timeout_is_sanitized(self):
        with patch.object(c.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = TimeoutError("private-request-data")
            with self.assertRaises(c.QueryError) as result:
                c.http_json("usage", "test", "test", 1)
            self.assertNotIn("private-request-data", str(result.exception))

    def test_invalid_json_is_sanitized(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'not-json-private-data'
        with patch.object(c.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = response
            with self.assertRaises(c.QueryError) as result:
                c.http_json("usage", "test", "test", 1)
            self.assertNotIn("private-data", str(result.exception))

    def test_ordinary_failure_does_not_refresh(self):
        with patch.object(c, "http_json", side_effect=c.QueryError("failure")), patch.object(c, "cli_limits") as cli:
            with self.assertRaises(c.QueryError):
                c.query(Path("/tmp"), valid_auth())
            cli.assert_not_called()

    def test_stale_token_routes_to_official_program(self):
        result = {"rateLimits": {"secondary": {"usedPercent": 20, "windowDurationMins": 10080}}}
        with patch.object(c, "cli_limits", return_value=result) as cli, patch.object(c, "http_json") as http:
            self.assertEqual(c.query(Path("/tmp"), {})["source"], "official-cli")
            cli.assert_called_once()
            http.assert_not_called()

    def test_protocol_handshake_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "fake-codex"
            executable.write_text('''#!/usr/bin/env python3
import json, sys
request = json.loads(sys.stdin.readline())
assert request['method'] == 'initialize'
print(json.dumps({'id': request['id'], 'result': {}}), flush=True)
assert json.loads(sys.stdin.readline())['method'] == 'initialized'
request = json.loads(sys.stdin.readline())
assert request['method'] == 'account/rateLimits/read'
print(json.dumps({'method':'notice','params':{}}), flush=True)
print(json.dumps({'id':request['id'],'result':{'rateLimits':{'primary':{'usedPercent':25}}}}), flush=True)
sys.stdin.read()
''')
            executable.chmod(0o700)
            with patch.object(c, "find_cli", return_value=str(executable)):
                self.assertEqual(c.cli_limits(Path(directory))["rateLimits"]["primary"]["usedPercent"], 25)


class CliDiscoveryTests(unittest.TestCase):
    def test_explicit_executable(self):
        with tempfile.TemporaryDirectory(prefix="meter cli ") as directory:
            executable = Path(directory) / "codex"
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(0o700)
            with patch.dict(os.environ, {"CODEX_METER_CLI": str(executable)}):
                self.assertEqual(c.find_cli(), str(executable))

    def test_invalid_override_does_not_silently_fallback(self):
        with patch.dict(os.environ, {"CODEX_METER_CLI": "relative/path"}), patch.object(c.shutil, "which") as search:
            with self.assertRaises(c.QueryError):
                c.find_cli()
            search.assert_not_called()


class CacheTests(unittest.TestCase):
    def test_three_screens_and_account_change(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "auth.json").write_text(json.dumps(valid_auth()))
            with patch.dict(os.environ, {"CODEX_HOME": directory, "XDG_CACHE_HOME": directory}), patch.object(c, "query", return_value=c.normalize(usage())) as query:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    results = list(pool.map(lambda _: c.collect(), range(3)))
                self.assertEqual(query.call_count, 1)
                self.assertEqual(results[0], results[2])
                data = valid_auth()
                data["tokens"]["account_id"] = "other-test-account"
                (home / "auth.json").write_text(json.dumps(data))
                c.collect()
                self.assertEqual(query.call_count, 2)
                cache = home / "codex-meter/usage.json"
                self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("access_token", cache.read_text())
                self.assertNotIn("other-test-account", cache.read_text())

    def test_custom_host_stops_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.toml").write_text('chatgpt_base_url = "https://example.test"')
            with patch.dict(os.environ, {"CODEX_HOME": directory}), patch.object(c, "query") as query:
                with self.assertRaises(c.QueryError):
                    c.collect()
                query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
