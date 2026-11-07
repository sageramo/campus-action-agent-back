"""Local development guard, not a public authentication or billing system."""
from collections import deque
import ipaddress
import os
import threading
import time


def local_extraction_allowed(client_ip: str, origin: str | None) -> bool:
    try:
        if not ipaddress.ip_address(client_ip).is_loopback:
            return False
    except ValueError:
        return False
    defaults = "http://127.0.0.1:8873,http://localhost:8873"
    allowed = {value.strip() for value in os.environ.get("SIXU_MODEL_ALLOWED_ORIGINS", defaults).split(",") if value.strip()}
    # Absence of Origin is allowed only from the real loopback peer (CLI/tests).
    return origin is None or origin in allowed


class ExtractionBudget:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.events = deque()
        self.active = False

    def acquire(self) -> str | None:
        with self.lock:
            now = self.clock()
            while self.events and self.events[0] <= now - 3600:
                self.events.popleft()
            if self.active:
                return "已有一份通知正在调用模型，请等待完成后再试。"
            if len(self.events) >= 60 or sum(value > now - 60 for value in self.events) >= 6:
                return "模型提取已达本地请求上限（每分钟6次、每小时60次），请稍后再试。"
            self.events.append(now)
            self.active = True
            return None

    def release(self) -> None:
        with self.lock:
            self.active = False


EXTRACTION_BUDGET = ExtractionBudget()


# ---- Public access to the model features (GitHub Pages -> Render) ---------------------------------
# Off unless SIXU_MODEL_ACCESS_CODE is set on the server. Then a request from an allowed page origin
# must carry the code in X-Sixu-Access; wrong codes are counted per IP and lock that IP for an hour
# after 10 tries; successful calls are capped per Beijing day for the whole site and for each IP, so
# the worst-case spend is bounded. The Origin header alone is not trusted (it can be forged); the
# code and the caps are the protection.
import hmac
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
DEFAULT_PUBLIC_ORIGINS = "https://sageramo.github.io"


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


class PublicModelGate:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.day = None
        self.total = 0
        self.by_ip: dict[str, int] = {}
        self.failures: dict[str, deque] = {}

    @staticmethod
    def code() -> str:
        return os.environ.get("SIXU_MODEL_ACCESS_CODE", "").strip()

    def configured(self) -> bool:
        return len(self.code()) >= 6

    def check(self, client_ip: str, origin: str | None, supplied: str | None) -> tuple[str, int, str] | None:
        """None when the call may go ahead; otherwise (message, HTTP status, error code)."""
        if not self.configured():
            return ("模型功能目前只在本机开放；线上使用需要作者在服务器上开启访问口令。", 403, "local_access_required")
        allowed = {value.strip() for value in os.environ.get("SIXU_PUBLIC_ORIGINS", DEFAULT_PUBLIC_ORIGINS).split(",") if value.strip()}
        if origin not in allowed:
            return ("这个页面地址不能使用模型功能。", 403, "origin_not_allowed")
        with self.lock:
            now = self.clock()
            fails = self.failures.setdefault(client_ip, deque())
            while fails and fails[0] <= now - 3600:
                fails.popleft()
            if len(fails) >= 10:
                return ("口令错误次数过多，请一小时后再试。", 429, "access_code_locked")
            if not supplied:
                return ("使用模型功能需要访问口令。", 401, "access_code_required")
            if not hmac.compare_digest(supplied.strip().encode("utf-8"), self.code().encode("utf-8")):
                fails.append(now)
                return ("访问口令不对，请重新输入。", 401, "access_code_invalid")
            day = datetime.fromtimestamp(now, BEIJING).date()
            if day != self.day:
                self.day, self.total, self.by_ip = day, 0, {}
            if self.total >= _int_env("SIXU_MODEL_DAILY_LIMIT", 200):
                return ("今天全站的模型调用次数已用完，明天再试，或先用规则提取。", 429, "model_daily_limit")
            if self.by_ip.get(client_ip, 0) >= _int_env("SIXU_MODEL_IP_DAILY_LIMIT", 30):
                return ("你今天的模型调用次数已用完，明天再试，或先用规则提取。", 429, "model_ip_daily_limit")
            self.total += 1
            self.by_ip[client_ip] = self.by_ip.get(client_ip, 0) + 1
            return None

    def status(self) -> dict[str, bool]:
        return {"code_required": self.configured()}


PUBLIC_MODEL_GATE = PublicModelGate()


def forwarded_client_ip(peer: str, forwarded_for: str | None) -> str:
    """The address the hosting proxy saw: the last X-Forwarded-For entry (it appends the real peer)."""
    if forwarded_for:
        last = forwarded_for.split(",")[-1].strip()
        if last:
            return last
    return peer
