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


def reset_schedule(state="pending", kind="reset_scheduled", precision="datetime", scope=None):
    return {"ok": True, "data": {
        "id": "schedule-1",
        "kind": kind,
        "resetType": "global",
        "effectiveAt": "2026-09-27T07:00:00.000Z",
        "scope": scope if scope is not None else {"plans": ["all"], "windows": ["five_hour"]},
        "schedulePrecision": precision,
        "scheduleBasis": "explicit",
        "scheduleWindow": {
            "startAt": "2026-09-27T07:00:00.000Z",
            "endAt": "2026-09-27T07:00:00.000Z"},
        "scheduleState": state,
        "confidence": 0.97,
    }, "meta": {"generatedAt": "2026-09-26T06:00:00.000Z",
                "lastSuccessfulCheckAt": "2026-09-26T05:59:00.000Z"}}


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


class ResetScheduleTests(unittest.TestCase):
    def test_pending_schedule_keeps_display_fields(self):
        result = c.normalize_reset_schedule(reset_schedule(scope={
            "plans": ["all"], "windows": ["five_hour", "weekly"]}))
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["schedule"]["precision"], "datetime")
        self.assertEqual(result["schedule"]["basis"], "explicit")
        self.assertEqual(result["schedule"]["confidence"], 0.97)
        self.assertEqual(result["schedule"]["scope"]["windows"], ["five_hour", "weekly"])
        self.assertEqual(result["meta"]["generatedAt"], "2026-09-26T06:00:00.000Z")

    def test_date_precision_and_unknown_scope_are_preserved_safely(self):
        record = reset_schedule(precision="date", scope={"plans": ["unknown"], "windows": ["unknown"]})
        record["data"]["scheduleWindow"] = {"startAt": "2026-09-27T00:00:00.000Z",
                                             "endAt": "2026-09-27T23:59:59.999Z"}
        result = c.normalize_reset_schedule(record)
        self.assertEqual(result["schedule"]["precision"], "date")
        self.assertEqual(result["schedule"]["scope"]["plans"], ["unknown"])
        self.assertEqual(result["schedule"]["window"]["startAt"], "2026-09-27T00:00:00.000Z")

    def test_only_pending_schedule_is_displayed_as_upcoming(self):
        for payload in (reset_schedule(state="fulfilled"), reset_schedule(state="elapsed"),
                        reset_schedule(state="unknown"), reset_schedule(kind="reset_completed")):
            self.assertEqual(c.normalize_reset_schedule(payload)["status"], "none")
        self.assertEqual(c.normalize_reset_schedule({"ok": True, "data": None})["status"], "none")

    def test_recent_elapsed_schedule_is_kept_as_unconfirmed_grace_signal(self):
        payload = reset_schedule(state="elapsed", precision="date")
        payload["data"]["effectiveAt"] = "2026-09-25T07:00:00.000Z"
        payload["data"]["scheduleWindow"] = {
            "startAt": "2026-09-25T07:00:00.000Z",
            "endAt": "2026-09-26T07:00:00.000Z",
        }
        now = c.parse_http_time("2026-09-26T07:55:00.000Z")
        result = c.normalize_reset_schedule(payload, now=now)
        self.assertEqual(result["status"], "grace")
        self.assertEqual(result["schedule"]["precision"], "date")

    def test_elapsed_schedule_is_not_kept_after_confirmation_grace(self):
        payload = reset_schedule(state="elapsed", precision="date")
        payload["data"]["effectiveAt"] = "2026-09-25T07:00:00.000Z"
        payload["data"]["scheduleWindow"] = {
            "startAt": "2026-09-25T07:00:00.000Z",
            "endAt": "2026-09-26T07:00:00.000Z",
        }
        now = c.parse_http_time("2026-09-26T10:00:00.000Z")
        self.assertEqual(c.normalize_reset_schedule(payload, now=now)["status"], "none")

    def test_invalid_api_response_is_an_error_not_no_schedule(self):
        for payload in ({"ok": False, "error": "data_unavailable"}, {"ok": True, "data": []}):
            with self.assertRaises(c.ResetApiError):
                c.normalize_reset_schedule(payload)


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

    def test_public_schedule_request_has_no_account_credentials(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"ok": True, "data": None}).encode()
        response.__enter__.return_value.headers = {"X-RateLimit-Remaining": "19"}
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(c.urllib.request, "build_opener", return_value=opener):
            c.reset_api_json(timeout=5)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, c.RESET_API_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Cookie"))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 5)

    def test_public_schedule_429_uses_retry_after(self):
        error = urllib.error.HTTPError(c.RESET_API_URL, 429, "limited", {
            "Retry-After": "120", "X-RateLimit-Remaining": "0"}, io.BytesIO(b'{"retryAfter":120}'))
        with patch.object(c.time, "time", return_value=1000), \
                patch.object(c.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = error
            with self.assertRaises(c.ResetApiError) as result:
                c.reset_api_json()
        self.assertTrue(result.exception.rate_limited)
        self.assertEqual(result.exception.retry_at, 1120)

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
            with patch.dict(os.environ, {"CODEX_HOME": directory, "XDG_CACHE_HOME": directory}), \
                    patch.object(c, "query", return_value=c.normalize(usage())) as query, \
                    patch.object(c, "reset_api_json", return_value=(
                        {"ok": True, "data": None}, {"remaining": 19, "resetAt": None})) as reset_api:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    results = list(pool.map(lambda _: c.collect(), range(3)))
                self.assertEqual(query.call_count, 1)
                self.assertEqual(reset_api.call_count, 1)
                self.assertEqual(results[0], results[2])
                data = valid_auth()
                data["tokens"]["account_id"] = "other-test-account"
                (home / "auth.json").write_text(json.dumps(data))
                c.collect()
                self.assertEqual(query.call_count, 2)
                self.assertEqual(reset_api.call_count, 1)
                cache = home / "codex-meter/usage.json"
                self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("access_token", cache.read_text())
                self.assertNotIn("other-test-account", cache.read_text())

    def test_custom_host_stops_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.toml").write_text('chatgpt_base_url = "https://example.test"')
            with patch.dict(os.environ, {"CODEX_HOME": directory, "XDG_CACHE_HOME": directory}), \
                    patch.object(c, "query") as query, patch.object(c, "reset_api_json", return_value=(
                        {"ok": True, "data": None}, {"remaining": 19, "resetAt": None})) as reset_api:
                result = c.collect()
                query.assert_not_called()
                reset_api.assert_called_once()
                self.assertFalse(result["ok"])
                self.assertEqual(result["resetSchedule"]["status"], "none")


class ResetScheduleCacheTests(unittest.TestCase):
    def test_elapsed_grace_signal_is_shared_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reset-schedule.json"
            payload = reset_schedule(state="elapsed", precision="date")
            payload["data"]["effectiveAt"] = "2026-09-25T07:00:00.000Z"
            payload["data"]["scheduleWindow"] = {
                "startAt": "2026-09-25T07:00:00.000Z",
                "endAt": "2026-09-26T07:00:00.000Z",
            }
            now = c.parse_http_time("2026-09-26T07:55:00.000Z")
            with patch.object(c, "reset_api_json", return_value=(
                    payload, {"remaining": 19, "resetAt": None})) as fetch:
                first = c.reset_schedule_status(path, now=now)
                cached = c.reset_schedule_status(path, now=now + 1)
            self.assertEqual(first["status"], "grace")
            self.assertEqual(cached["status"], "grace")
            self.assertEqual(fetch.call_count, 1)

    def test_automatic_cache_manual_bypass_and_ten_second_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reset-schedule.json"
            response = reset_schedule()
            limits = {"remaining": 19, "resetAt": None}
            with patch.object(c, "reset_api_json", return_value=(response, limits)) as fetch:
                first = c.reset_schedule_status(path, now=1000)
                automatic = c.reset_schedule_status(path, now=1001)
                deduplicated = c.reset_schedule_status(path, force=True, now=1005)
                forced = c.reset_schedule_status(path, force=True, now=1011)
            self.assertEqual(first["status"], "pending")
            self.assertEqual(automatic["status"], "pending")
            self.assertEqual(deduplicated["status"], "pending")
            self.assertEqual(forced["status"], "pending")
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rate_limit_cooldown_blocks_manual_bypass_and_keeps_fresh_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reset-schedule.json"
            response = reset_schedule()
            limits = {"remaining": 0, "resetAt": 1300}
            with patch.object(c, "reset_api_json", return_value=(response, limits)) as fetch:
                c.reset_schedule_status(path, now=1000)
                cached = c.reset_schedule_status(path, force=True, now=1011)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(cached["status"], "pending")
            self.assertFalse(cached["stale"])

    def test_zero_remaining_without_reset_header_uses_safe_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reset-schedule.json"
            response = reset_schedule()
            with patch.object(c, "reset_api_json", return_value=(
                    response, {"remaining": 0, "resetAt": None})) as fetch:
                c.reset_schedule_status(path, force=True, now=1000)
                waiting = c.reset_schedule_status(path, force=True, now=1011)
                refreshed = c.reset_schedule_status(path, force=True, now=1601)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(waiting["status"], "pending")
            self.assertEqual(refreshed["status"], "pending")

    def test_429_retry_after_blocks_manual_query_until_server_allows_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reset-schedule.json"
            response = reset_schedule()
            with patch.object(c, "reset_api_json", side_effect=[
                    c.ResetApiError("限流", retry_at=1300, remaining=0, rate_limited=True),
                    (response, {"remaining": 19, "resetAt": None})]) as fetch:
                first = c.reset_schedule_status(path, force=True, now=1000)
                waiting = c.reset_schedule_status(path, force=True, now=1011)
                refreshed = c.reset_schedule_status(path, force=True, now=1301)
            self.assertEqual(first["status"], "unavailable")
            self.assertEqual(waiting["status"], "unavailable")
            self.assertEqual(refreshed["status"], "pending")
            self.assertEqual(fetch.call_count, 2)

    def test_failed_refresh_preserves_previous_schedule_as_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reset-schedule.json"
            response = reset_schedule()
            with patch.object(c, "reset_api_json", side_effect=[
                    (response, {"remaining": 19, "resetAt": None}),
                    c.ResetApiError("离线")]):
                first = c.reset_schedule_status(path, now=1000)
                stale = c.reset_schedule_status(path, force=True, now=1011)
            self.assertEqual(first["status"], "pending")
            self.assertEqual(stale["status"], "pending")
            self.assertTrue(stale["stale"])
            self.assertEqual(stale["message"], "离线")

    def test_schedule_failure_does_not_replace_quota_result(self):
        with tempfile.TemporaryDirectory() as directory:
            windows = [{"label": "周额度", "used": 98,
                        "reset": "2026-09-26T09:00:00.000Z", "seconds": 604800, "group": "Codex"}]
            with patch.dict(os.environ, {"CODEX_HOME": directory, "XDG_CACHE_HOME": directory}), \
                    patch.object(c, "quota_result", return_value={"ok": True, "windows": windows}), \
                    patch.object(c, "reset_api_json", side_effect=c.ResetApiError("offline")):
                result = c.collect()
            self.assertTrue(result["ok"])
            self.assertEqual(result["windows"][0]["reset"], windows[0]["reset"])
            self.assertEqual(result["resetSchedule"]["status"], "unavailable")

    def test_no_public_schedule_keeps_account_window_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            windows = [{"label": "周额度", "used": 98,
                        "reset": "2026-09-26T09:00:00.000Z", "seconds": 604800, "group": "Codex"}]
            with patch.dict(os.environ, {"CODEX_HOME": directory, "XDG_CACHE_HOME": directory}), \
                    patch.object(c, "quota_result", return_value={"ok": True, "windows": windows}), \
                    patch.object(c, "reset_api_json", return_value=(
                        {"ok": True, "data": None}, {"remaining": 19, "resetAt": None})):
                result = c.collect()
            self.assertEqual(result["windows"][0]["reset"], windows[0]["reset"])
            self.assertEqual(result["resetSchedule"]["status"], "none")


if __name__ == "__main__":
    unittest.main()
