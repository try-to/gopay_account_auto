"""Hero-SMS 接码服务客户端（简化版）

核心功能：
- get_number: 购买号码
- poll_code: 轮询验证码
- set_status: 设置激活状态（完成/取消/重发）
- SmsActivation: 验证码生命周期管理
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Optional

try:
    from curl_cffi import requests as _requests
    _CurlCffiAvailable = True
except ImportError:
    import requests as _requests
    _CurlCffiAvailable = False

logger = logging.getLogger(__name__)

# 状态常量
STATUS_READY = 1       # 标记就绪（准备接码）
STATUS_CANCEL = -1     # 取消激活
STATUS_RESEND = 3      # 请求重发
STATUS_FINISH = 6      # 完成（确认收到码）

POLL_INTERVAL_SEC = 3.0


class HeroSmsError(RuntimeError):
    pass


def _request(
    base_url: str,
    api_key: str,
    action: str,
    params: Optional[dict] = None,
    timeout: int = 25,
) -> tuple[bool, str, Any]:
    """发送 hero-sms API 请求"""
    if not api_key:
        return False, "NO_KEY", None

    query = {"action": action, "api_key": api_key}
    if params:
        for k, v in params.items():
            if v is not None and str(v).strip():
                query[k] = v

    try:
        kwargs = {"params": query, "timeout": timeout}
        if _CurlCffiAvailable:
            kwargs["impersonate"] = "chrome131"
        resp = _requests.get(base_url, **kwargs)
    except Exception as e:
        return False, f"REQUEST_ERROR:{e}", None

    code = resp.status_code
    text = resp.text.strip()
    try:
        data = resp.json()
    except Exception:
        data = None

    if not (200 <= code < 300):
        return False, text or f"HTTP {code}", data
    return True, text, data


def get_balance(base_url: str, api_key: str) -> tuple[float, str]:
    """查询余额"""
    ok, text, data = _request(base_url, api_key, "getBalance", timeout=20)
    if not ok:
        return -1.0, str(text or "getBalance failed")

    line = str(text or "").strip()
    if line.upper().startswith("ACCESS_BALANCE:"):
        raw = line.split(":", 1)[1].strip()
        try:
            return float(raw), ""
        except Exception:
            pass

    if isinstance(data, dict):
        for val in [data.get("balance"), data.get("amount"), data.get("data")]:
            try:
                num = float(val.get("balance") or val.get("amount") or -1) if isinstance(val, dict) else float(val)
            except Exception:
                continue
            if num >= 0:
                return num, ""

    return -1.0, line or "无法解析余额"


def set_status(base_url: str, api_key: str, activation_id: str, status: int) -> str:
    """设置激活状态"""
    if not activation_id:
        return ""
    _, text, _ = _request(base_url, api_key, "setStatus", {"id": activation_id, "status": status}, timeout=20)
    return str(text or "")


def mark_ready(activation_id: str) -> None:
    """标记就绪（从配置读取）"""
    pass


def finish(activation_id: str) -> None:
    """标记完成（从配置读取）"""
    pass


def cancel(activation_id: str) -> None:
    """取消激活（从配置读取）"""
    pass


def get_number(
    service_code: str,
    country_id: int,
    base_url: str,
    api_key: str,
    max_price: float = 10.0,
    log: Callable[[str], None] = logger.info,
) -> tuple[str, str, str]:
    """购买号码

    返回: (activation_id, phone, error)
    """
    ok, text, data = _request(
        base_url,
        api_key,
        "getNumber",
        {"service": service_code, "country": country_id},
        timeout=30,
    )

    if not ok:
        return "", "", str(text or "getNumber failed")

    line = str(text or "").strip()
    if line.upper().startswith("ACCESS_NUMBER:"):
        parts = line.split(":", 2)
        if len(parts) >= 3:
            activation_id = parts[1].strip()
            phone = parts[2].strip()
            return activation_id, phone, ""

    if isinstance(data, dict):
        activation_id = str(data.get("activationId") or data.get("id") or "")
        phone = str(data.get("phoneNumber") or data.get("phone") or "")
        if activation_id and phone:
            return activation_id, phone, ""

    return "", "", line or "无法解析号码"


def poll_code(
    activation_id: str,
    base_url: str,
    api_key: str,
    timeout_sec: int = 300,
    log: Callable[[str], None] = logger.info,
) -> str:
    """轮询验证码"""
    if not activation_id:
        return ""

    start = time.time()
    last_resend = start
    attempt = 0

    while time.time() - start < timeout_sec:
        attempt += 1
        ok, text, data = _request(base_url, api_key, "getStatus", {"id": activation_id}, timeout=20)

        if not ok:
            log(f"[poll_code] 请求失败: {text}")
            time.sleep(POLL_INTERVAL_SEC)
            continue

        line = str(text or "").strip()

        # 解析 STATUS_OK:code
        if line.upper().startswith("STATUS_OK:"):
            code = line.split(":", 1)[1].strip()
            if code:
                return code

        # 解析 JSON
        if isinstance(data, dict):
            code = str(data.get("code") or data.get("sms") or "")
            if code:
                return code

        # 超过 RESEND_AFTER_SEC 自动重发
        if time.time() - last_resend > RESEND_AFTER_SEC:
            log(f"[poll_code] 超过 {RESEND_AFTER_SEC}s 未收到码，请求重发")
            set_status(base_url, api_key, activation_id, STATUS_RESEND)
            last_resend = time.time()

        time.sleep(POLL_INTERVAL_SEC)

    return ""


class SmsActivation:
    """SMS 验证码生命周期管理"""

    def __init__(
        self,
        activation_id: str,
        phone: str,
        country_id: int,
        base_url: str,
        api_key: str,
        log: Callable[[str], None] = logger.info,
    ):
        self.activation_id = activation_id
        self.phone = phone
        self.country_id = country_id
        self.base_url = base_url
        self.api_key = api_key
        self.log = log
        self.used_codes: set[str] = set()

    def wait_code(self, timeout_sec: int = 300, label: str = "", max_resends: int = 3) -> str:
        """等待验证码（自动排除已用码，最多重发 max_resends 次，间隔递增）"""
        start = time.time()
        last_resend = start
        resend_count = 0
        resend_intervals = [30, 60, 120]  # 递增间隔：30s, 60s, 120s

        while time.time() - start < timeout_sec:
            ok, text, data = _request(
                self.base_url,
                self.api_key,
                "getStatus",
                {"id": self.activation_id},
                timeout=20,
            )

            if not ok:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            line = str(text or "").strip()
            code = ""

            if line.upper().startswith("STATUS_OK:"):
                code = line.split(":", 1)[1].strip()
            elif isinstance(data, dict):
                code = str(data.get("code") or data.get("sms") or "")

            if code and code not in self.used_codes:
                self.used_codes.add(code)
                return code

            # 自动重发（限制次数，间隔递增）
            elapsed = time.time() - last_resend
            if resend_count < max_resends:
                wait_time = resend_intervals[resend_count] if resend_count < len(resend_intervals) else resend_intervals[-1]
                if elapsed > wait_time:
                    resend_count += 1
                    self.log(f"[{label}] 超过 {wait_time}s 未收到新码，请求第 {resend_count} 次重发")
                    set_status(self.base_url, self.api_key, self.activation_id, STATUS_RESEND)
                    last_resend = time.time()
            elif elapsed > resend_intervals[-1] and resend_count == max_resends:
                self.log(f"[{label}] 已达到最大重发次数 ({max_resends})，停止重发")
                resend_count += 1  # 防止重复输出

            time.sleep(POLL_INTERVAL_SEC)

        return ""

    def cancel_activation(self) -> None:
        """取消激活"""
        if self.activation_id:
            set_status(self.base_url, self.api_key, self.activation_id, STATUS_CANCEL)
            self.log(f"[sms] 取消激活 {self.activation_id}")

    def release(self) -> None:
        """标记完成"""
        if self.activation_id:
            set_status(self.base_url, self.api_key, self.activation_id, STATUS_FINISH)
            self.log(f"[sms] 完成激活 {self.activation_id}")
