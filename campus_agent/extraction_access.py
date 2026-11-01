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
