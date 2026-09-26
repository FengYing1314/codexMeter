#!/usr/bin/env python3
"""读取 Codex 账号额度，仅输出界面需要的字段。仅依赖 Python 标准库。"""
import base64
import fcntl
import hashlib
import http.client
import json
import math
import os
import selectors
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

API_ROOT = "https://chatgpt.com/backend-api/wham/"
RESET_API_URL = "https://didcodexreset.com/openapi/v1/records/latest?kind=all"
MAX_BYTES = 2 * 1024 * 1024
CACHE_SECONDS = 10
RESET_CACHE_SECONDS = 10 * 60
RESET_MAX_BYTES = 512 * 1024
RESET_TIMEOUT = 5
RESET_SCHEDULE_GRACE_SECONDS = 3 * 60 * 60


class QueryError(Exception):
    """仅携带可展示的错误，不带请求头、响应原文或凭据。"""


class ResetApiError(QueryError):
    def __init__(self, message, retry_at=None, remaining=None, rate_limited=False):
        super().__init__(message)
        self.retry_at = retry_at
        self.remaining = remaining
        self.rate_limited = rate_limited


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def obj(value):
    return value if isinstance(value, dict) else {}


def jwt_claims(token):
    # JWT 只用于到期提示和账号选择，服务端负责验证签名与权限。
    try:
        part = token.split(".")[1]
        return obj(json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))))
    except (ValueError, IndexError, UnicodeError, AttributeError):
        return {}


def needs_refresh(auth, now):
    expiry = jwt_claims(obj(auth.get("tokens")).get("access_token", "")).get("exp")
    if number(expiry):
        return expiry <= now + 300
    try:
        refreshed = datetime.fromisoformat(auth["last_refresh"].replace("Z", "+00:00"))
        if refreshed.tzinfo is None:
            return True
        return now - refreshed.timestamp() >= 8 * 86400
    except (KeyError, TypeError, ValueError, AttributeError):
        return True


def load_auth(home):
    try:
        data = json.loads((home / "auth.json").read_text())
    except FileNotFoundError:
        return {}  # 官方程序还可能从系统凭据存储读取账号。
    except (OSError, ValueError):
        raise QueryError("无法读取 Codex 登录文件，请检查权限或重新登录") from None
    if not isinstance(data, dict):
        raise QueryError("Codex 登录文件格式异常")
    return data


def credentials(auth):
    tokens = obj(auth.get("tokens"))
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        raise QueryError("请先在 Codex 中登录 ChatGPT 账号")
    account = tokens.get("account_id")
    if not account:
        for token in (access, tokens.get("id_token", "")):
            account = obj(jwt_claims(token).get("https://api.openai.com/auth")).get("chatgpt_account_id")
            if account:
                break
    if not isinstance(account, str) or not account:
        raise QueryError("登录状态缺少账号标识，请重新登录 Codex")
    if any(c in access + account for c in "\r\n"):
        raise QueryError("登录凭据格式异常")
    return access, account


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 带凭据的请求只能访问固定官方地址。
        return None


def http_json(endpoint, access, account, timeout):
    request = urllib.request.Request(API_ROOT + endpoint, headers={
        "Authorization": "Bearer " + access,
        "ChatGPT-Account-Id": account,
        "Accept": "application/json",
        "User-Agent": "codex-meter/1.1",
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise QueryError("额度响应过大")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise QueryError("额度响应格式异常")
        return value
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        if status in (401, 403):
            raise QueryError("登录已失效或无权查询，请在 Codex 中重新登录") from None
        if status == 429:
            raise QueryError("查询过于频繁，请稍后重试") from None
        raise QueryError("额度服务暂不可用（HTTP " + str(status) + "）") from None
    except (TimeoutError, urllib.error.URLError):
        raise QueryError("网络连接失败或查询超时") from None
    except (ValueError, UnicodeError):
        raise QueryError("额度响应格式异常") from None


def parse_http_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def retry_after_time(value, now):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return parse_http_time(value)
    if not math.isfinite(seconds):
        return None
    return now + max(0, seconds)


def rate_limit_reset_time(value, now):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        timestamp = float(value.strip())
    except ValueError:
        return parse_http_time(value)
    if not math.isfinite(timestamp):
        return None
    # 文档将该头定义为 UTC 时间；兼容以秒为单位的倒计时格式。
    return timestamp if timestamp >= 1_000_000_000 else now + max(0, timestamp)


def rate_limit_headers(headers, now):
    headers = headers or {}
    remaining = None
    try:
        raw_remaining = headers.get("X-RateLimit-Remaining")
        if isinstance(raw_remaining, str):
            parsed_remaining = int(raw_remaining)
            if parsed_remaining >= 0:
                remaining = parsed_remaining
    except (TypeError, ValueError):
        pass
    return {
        "remaining": remaining,
        "resetAt": rate_limit_reset_time(headers.get("X-RateLimit-Reset"), now),
    }


def reset_api_json(timeout=RESET_TIMEOUT):
    request = urllib.request.Request(RESET_API_URL, headers={
        "Accept": "application/json",
        "User-Agent": "codex-meter/1.1",
    })
    now = time.time()
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            limits = rate_limit_headers(response.headers, now)
            raw = response.read(RESET_MAX_BYTES + 1)
        if len(raw) > RESET_MAX_BYTES:
            raise ResetApiError("公开排期响应过大", limits["resetAt"] if limits["remaining"] == 0 else None,
                                limits["remaining"])
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise ResetApiError("公开排期响应格式异常", limits["resetAt"] if limits["remaining"] == 0 else None,
                                limits["remaining"]) from None
        return payload, limits
    except urllib.error.HTTPError as error:
        status = error.code
        error_headers = error.headers or {}
        limits = rate_limit_headers(error_headers, now)
        retry_at = retry_after_time(error_headers.get("Retry-After"), now)
        if retry_at is None and limits["remaining"] == 0:
            retry_at = limits["resetAt"]
        body = b""
        if status == 429:
            try:
                body = error.read(RESET_MAX_BYTES + 1)
            except (OSError, http.client.HTTPException):
                pass
        error.close()
        if status == 429:
            try:
                error_data = json.loads(body) if len(body) <= RESET_MAX_BYTES else {}
            except (ValueError, UnicodeError, RecursionError):
                error_data = {}
            if retry_at is None:
                retry_at = retry_after_time(str(obj(error_data).get("retryAfter", "")), now)
            raise ResetApiError("公开排期服务请求过于频繁", retry_at or now + RESET_CACHE_SECONDS,
                                limits["remaining"], rate_limited=True) from None
        if status in (401, 403):
            message = "公开排期服务拒绝了请求"
        else:
            message = "公开排期服务暂不可用（HTTP " + str(status) + "）"
        if retry_at is None and limits["remaining"] == 0:
            retry_at = now + RESET_CACHE_SECONDS
        raise ResetApiError(message, retry_at, limits["remaining"], rate_limited=retry_at is not None) from None
    except (TimeoutError, urllib.error.URLError, http.client.HTTPException, OSError):
        raise ResetApiError("连接公开排期服务失败或查询超时", now + RESET_CACHE_SECONDS) from None


def safe_timestamp(value):
    if not isinstance(value, str) or not value or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None and "T" in value:
        return None
    return value


def safe_codes(value):
    if not isinstance(value, list):
        return []
    codes = []
    for item in value[:10]:
        if not isinstance(item, str):
            continue
        code = "".join(char for char in item if char.isalnum() or char in "_-.")[:48]
        if code:
            codes.append(code)
    return codes


def normalize_reset_schedule(payload, now=None):
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ResetApiError("公开排期服务尚无可用数据")
    meta = obj(payload.get("meta"))
    normalized_meta = {
        "generatedAt": safe_timestamp(meta.get("generatedAt")),
        "lastSuccessfulCheckAt": safe_timestamp(meta.get("lastSuccessfulCheckAt")),
    }
    record = payload.get("data")
    if record is None:
        return {"status": "none", "stale": False, "schedule": None,
                "meta": normalized_meta, "message": ""}
    if not isinstance(record, dict):
        raise ResetApiError("公开排期记录格式异常")
    schedule_state = record.get("scheduleState")
    if (record.get("kind") != "reset_scheduled"
            or schedule_state not in ("pending", "elapsed")):
        return {"status": "none", "stale": False, "schedule": None,
                "meta": normalized_meta, "message": ""}

    raw_scope = record.get("scope")
    scope = None
    if isinstance(raw_scope, dict):
        scope = {"plans": safe_codes(raw_scope.get("plans")),
                 "windows": safe_codes(raw_scope.get("windows"))}
    raw_window = record.get("scheduleWindow")
    schedule_window = None
    if isinstance(raw_window, dict):
        start_at = safe_timestamp(raw_window.get("startAt"))
        end_at = safe_timestamp(raw_window.get("endAt"))
        if start_at or end_at:
            schedule_window = {"startAt": start_at, "endAt": end_at}
    status = "pending"
    if schedule_state == "elapsed":
        schedule_end = parse_http_time((schedule_window or {}).get("endAt"))
        if (schedule_end is None or schedule_end > now
                or now - schedule_end >= RESET_SCHEDULE_GRACE_SECONDS):
            return {"status": "none", "stale": False, "schedule": None,
                    "meta": normalized_meta, "message": ""}
        status = "grace"
    confidence = record.get("confidence")
    if not number(confidence) or not 0 <= confidence <= 1:
        confidence = None
    schedule = {
        "resetType": record.get("resetType") if record.get("resetType") in
                    ("global", "banked", "global_and_banked") else "unknown",
        "effectiveAt": safe_timestamp(record.get("effectiveAt")),
        "precision": record.get("schedulePrecision") if record.get("schedulePrecision") in
                    ("date", "datetime") else "unknown",
        "basis": record.get("scheduleBasis") if record.get("scheduleBasis") in
                 ("explicit", "contextual_inference") else "unknown",
        "confidence": confidence,
        "scope": scope,
        "window": schedule_window,
    }
    return {"status": status, "stale": False, "schedule": schedule,
            "meta": normalized_meta, "message": ""}


def load_reset_cache(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_reset_cache(path, value):
    with path.open("w") as file:
        os.fchmod(file.fileno(), 0o600)
        json.dump(value, file, ensure_ascii=False)


def cached_reset_status(cache, now, stale=False):
    last_success = cache.get("lastSuccess")
    if isinstance(last_success, dict) and last_success.get("status") in ("pending", "grace", "none"):
        result = dict(last_success)
        try:
            last_success_at = float(cache.get("lastSuccessAt"))
        except (TypeError, ValueError):
            last_success_at = 0
        too_old = not last_success_at or now - last_success_at >= RESET_CACHE_SECONDS
        result["stale"] = stale or bool(cache.get("lastError")) or too_old
        result["message"] = cache.get("lastError", "") if result["stale"] else ""
        if result["stale"] and not result["message"]:
            result["message"] = "公开排期数据已过缓存周期，等待限流窗口恢复。"
        return result
    return {"status": "unavailable", "stale": False, "schedule": None,
            "meta": {}, "message": cache.get("lastError") or "公开排期暂不可用"}


def reset_schedule_status(path, force=False, now=None):
    now = time.time() if now is None else now
    cache = load_reset_cache(path)
    last_attempt = cache.get("lastAttemptAt")
    try:
        last_attempt = float(last_attempt)
    except (TypeError, ValueError):
        last_attempt = None
    if last_attempt is not None and 0 <= now - last_attempt < CACHE_SECONDS:
        return cached_reset_status(cache, now)

    rate_limited_until = cache.get("rateLimitedUntil")
    try:
        rate_limited_until = float(rate_limited_until)
    except (TypeError, ValueError):
        rate_limited_until = None
    if rate_limited_until is not None and rate_limited_until > now:
        return cached_reset_status(cache, now)

    next_automatic_at = cache.get("nextAutomaticAt")
    try:
        next_automatic_at = float(next_automatic_at)
    except (TypeError, ValueError):
        next_automatic_at = None
    if not force and next_automatic_at is not None and next_automatic_at > now:
        return cached_reset_status(cache, now)

    try:
        payload, limits = reset_api_json()
        try:
            result = normalize_reset_schedule(payload, now=now)
        except ResetApiError as error:
            error.remaining = limits["remaining"]
            if limits["remaining"] == 0:
                error.retry_at = limits["resetAt"] or now + RESET_CACHE_SECONDS
                error.rate_limited = True
            raise
        cache.update({
            "lastSuccess": result,
            "lastSuccessAt": now,
            "lastError": "",
            "lastAttemptAt": now,
            "nextAutomaticAt": now + RESET_CACHE_SECONDS,
            "rateLimitedUntil": limits["resetAt"] if limits["remaining"] == 0 else None,
        })
        if limits["remaining"] == 0 and not cache["rateLimitedUntil"]:
            cache["rateLimitedUntil"] = now + RESET_CACHE_SECONDS
    except ResetApiError as error:
        cache["lastError"] = str(error)
        cache["lastAttemptAt"] = now
        cache["nextAutomaticAt"] = now + RESET_CACHE_SECONDS
        cache["rateLimitedUntil"] = error.retry_at if error.rate_limited or error.remaining == 0 else None
        if error.remaining == 0 and cache["rateLimitedUntil"] is None:
            cache["rateLimitedUntil"] = now + RESET_CACHE_SECONDS
        if cache["rateLimitedUntil"] is not None and cache["rateLimitedUntil"] <= now:
            cache["rateLimitedUntil"] = now + RESET_CACHE_SECONDS
    except QueryError as error:
        cache["lastError"] = str(error)
        cache["lastAttemptAt"] = now
        cache["nextAutomaticAt"] = now + RESET_CACHE_SECONDS
        cache["rateLimitedUntil"] = None
    try:
        write_reset_cache(path, cache)
    except OSError:
        pass
    return cached_reset_status(cache, now, stale=bool(cache.get("lastError")))


def iso_time(value):
    if not number(value) or value <= 0:
        return ""
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def window(value, group, rpc=False):
    value = obj(value)
    used = value.get("usedPercent" if rpc else "used_percent")
    if not number(used):
        return None
    duration = value.get("windowDurationMins" if rpc else "limit_window_seconds")
    seconds = duration * (60 if rpc else 1) if number(duration) and duration > 0 else None
    label = "周额度" if seconds == 604800 else "5 小时额度" if seconds == 18000 else "短周期额度" if seconds and seconds < 86400 else "其他额度"
    if group != "Codex":
        label = group + " · " + label
    return {"label": label, "used": max(0, min(100, used)),
            "reset": iso_time(value.get("resetsAt" if rpc else "reset_at")),
            "seconds": seconds, "group": group}


def bucket_windows(bucket, group, rpc=False):
    names = ("primary", "secondary") if rpc else ("primary_window", "secondary_window")
    items = [window(obj(bucket).get(key), group, rpc) for key in names]
    # 按实际周期排序，不能假设服务端 primary 一定是短周期。
    return sorted((item for item in items if item), key=lambda item: item["seconds"] or math.inf)


def display_name(value, default):
    return "".join(c for c in value if c.isprintable())[:64] if isinstance(value, str) and value else default


def reset_count(value, rpc=False):
    count = obj(value).get("availableCount" if rpc else "available_count")
    return count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None


def normalize(payload, rpc=False):
    payload = obj(payload)
    if rpc:
        buckets = obj(payload.get("rateLimitsByLimitId"))
        main = buckets.get("codex", payload.get("rateLimits"))
        windows = bucket_windows(main, "Codex", True)
        for key, value in buckets.items():
            if key == "codex" or value == main:
                continue
            name = display_name(obj(value).get("limitName"), display_name(key, "额外额度"))
            windows.extend(bucket_windows(value, name, True))
        credits = reset_count(payload.get("rateLimitResetCredits"), True)
    else:
        windows = bucket_windows(payload.get("rate_limit"), "Codex")
        for entry in payload.get("additional_rate_limits") or []:
            if isinstance(entry, dict):
                windows.extend(bucket_windows(entry.get("rate_limit"), display_name(entry.get("limit_name"), "额外额度")))
        credits = reset_count(payload.get("rate_limit_reset_credits"))
    if not windows:
        raise QueryError("账号暂未返回可显示的额度")
    return {"ok": True, "windows": windows, "resetCredits": credits,
            "source": "official-cli" if rpc else "direct", "updatedAt": iso_time(time.time())}


def find_cli():
    configured = os.environ.get("CODEX_METER_CLI")
    if configured:
        executable = Path(configured).expanduser()
        if executable.is_absolute() and executable.is_file() and os.access(executable, os.X_OK):
            return str(executable)
        raise QueryError("CODEX_METER_CLI 必须指向可执行文件的绝对路径")
    located = shutil.which("codex")
    if located:
        return located
    # 兼容将 Codex 捆绑在桌面应用中的 Linux 安装布局。
    bundled = Path("/usr/lib/chatgpt/resources/codex")
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    raise QueryError("登录状态需要刷新，请打开 Codex 重新登录；未找到官方命令行程序")


def cli_limits(home):
    env = dict(os.environ, CODEX_HOME=str(home))
    process = subprocess.Popen([find_cli(), "-s", "read-only", "-a", "never", "app-server"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=env, cwd=str(home))
    pending = b""
    deadline = time.monotonic() + 40

    def send(value):
        process.stdin.write((json.dumps(value) + "\n").encode())
        process.stdin.flush()

    def receive(request_id, selector):
        nonlocal pending
        while time.monotonic() < deadline:
            if b"\n" not in pending:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                block = os.read(process.stdout.fileno(), 65536)
                if not block:
                    raise QueryError("官方认证程序提前退出，请检查 Codex 登录状态")
                pending += block
                if len(pending) > MAX_BYTES:
                    raise QueryError("官方程序响应过大")
                continue
            line, pending = pending.split(b"\n", 1)
            try:
                message = json.loads(line)
            except ValueError:
                raise QueryError("官方程序响应格式异常") from None
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported request"}})
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise QueryError("官方程序无法读取额度，请检查 Codex 登录状态")
                return obj(message.get("result"))
        raise QueryError("官方认证程序查询超时")

    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "codex_meter", "version": "1.1.0"}}})
            receive(1, selector)
            send({"method": "initialized"})
            send({"id": 2, "method": "account/rateLimits/read"})
            return receive(2, selector)
    finally:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def query(home, auth):
    # 不自行兑换共享刷新令牌；官方程序负责认证与刷新。
    if not obj(auth.get("tokens")).get("access_token") or needs_refresh(auth, time.time()):
        return normalize(cli_limits(home), rpc=True)
    access, account = credentials(auth)
    result = normalize(http_json("usage", access, account, 30))
    try:
        count = reset_count(http_json("rate-limit-reset-credits", access, account, 4))
        if count is not None:
            result["resetCredits"] = count
    except QueryError:
        pass  # 重置券不可用时，不影响已经取得的额度。
    return result


def quota_result(home, cache_dir):
    # 自定义服务地址可能指向其他账号体系，不能误用默认账号端点。
    config_file = home / "config.toml"
    try:
        if config_file.exists():
            config = tomllib.loads(config_file.read_text())
            if config.get("chatgpt_base_url"):
                raise QueryError("当前设置了自定义 ChatGPT 地址，本插件仅支持默认官方服务")
        auth = load_auth(home)
    except QueryError as error:
        return {"ok": False, "message": str(error)}
    except (OSError, ValueError, TypeError):
        return {"ok": False, "message": "无法读取本地配置，请检查文件格式和权限"}

    key = hashlib.sha256((str(home) + json.dumps(auth, sort_keys=True)).encode()).hexdigest()
    path = cache_dir / "usage.json"
    try:
        cached = json.loads(path.read_text())
        if (isinstance(cached, dict) and cached.get("key") == key
                and isinstance(cached.get("savedAt"), (int, float))
                and 0 <= time.time() - cached["savedAt"] < CACHE_SECONDS
                and isinstance(cached.get("result"), dict)):
            return cached["result"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        result = query(home, auth)
    except QueryError as error:
        result = {"ok": False, "message": str(error)}
    data = {"key": key, "savedAt": time.time(), "result": result}
    # 缓存只包含不可逆标识和过滤后的额度；不写入原始认证或响应。
    with path.open("w") as file:
        os.fchmod(file.fileno(), 0o600)
        json.dump(data, file, ensure_ascii=False)
    return result


def collect(force_reset_schedule=False):
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    cache_dir = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "codex-meter"
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 多屏共用短时结果，并串行调用官方程序，避免并发刷新同一份凭据。
    with (cache_dir / "query.lock").open("a") as lock:
        deadline = time.monotonic() + 45
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise QueryError("另一次额度查询尚未结束")
                time.sleep(0.1)
        result = quota_result(home, cache_dir)
        result = dict(result)
        result["resetSchedule"] = reset_schedule_status(
            cache_dir / "reset-schedule.json", force=force_reset_schedule)
        return result


def main():
    force_reset_schedule = "--force-reset-schedule" in sys.argv[1:]
    try:
        output = collect(force_reset_schedule=force_reset_schedule)
    except QueryError as error:
        output = {"ok": False, "message": str(error)}
    except (OSError, ValueError, TypeError):
        output = {"ok": False, "message": "无法完成查询，请检查本地配置、权限和登录状态"}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
