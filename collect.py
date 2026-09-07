#!/usr/bin/env python3
"""读取 Codex 账号额度，仅输出界面需要的字段。仅依赖 Python 标准库。"""
import base64
import fcntl
import hashlib
import json
import math
import os
import selectors
import shutil
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_ROOT = "https://chatgpt.com/backend-api/wham/"
MAX_BYTES = 2 * 1024 * 1024
CACHE_SECONDS = 10


class QueryError(Exception):
    """仅携带可展示的错误，不带请求头、响应原文或凭据。"""


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


def collect():
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    # 自定义服务地址可能指向其他账号体系，不能误用默认账号端点。
    config_file = home / "config.toml"
    if config_file.exists():
        config = tomllib.loads(config_file.read_text())
        if config.get("chatgpt_base_url"):
            raise QueryError("当前设置了自定义 ChatGPT 地址，本插件仅支持默认官方服务")
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
        auth = load_auth(home)
        key = hashlib.sha256((str(home) + json.dumps(auth, sort_keys=True)).encode()).hexdigest()
        path = cache_dir / "usage.json"
        try:
            cached = json.loads(path.read_text())
            if isinstance(cached, dict) and cached.get("key") == key and 0 <= time.time() - cached["savedAt"] < CACHE_SECONDS:
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


def main():
    try:
        output = collect()
    except QueryError as error:
        output = {"ok": False, "message": str(error)}
    except (OSError, ValueError, TypeError):
        output = {"ok": False, "message": "无法完成查询，请检查本地配置、权限和登录状态"}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
