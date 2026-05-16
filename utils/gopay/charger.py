"""GoPay 红包领取模块（简化版）

实现 GoPay 节日红包领取功能。
"""
from __future__ import annotations

import html
import json
import logging
import re
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse, unquote

import requests

logger = logging.getLogger(__name__)


class GoPayError(RuntimeError):
    pass


# ──────────────────────────── 辅助函数 ────────────────────────────


def _extract_js_var(page: str, name: str) -> str:
    """从页面中提取 JS 变量值"""
    match = re.search(
        rf"\bvar\s+{re.escape(name)}\s*=\s*(['\"])(.*?)\1\s*;",
        page or "",
        flags=re.DOTALL,
    )
    if not match:
        return ""
    value = match.group(2)
    # 解码 HTML 实体和 Unicode 转义
    value = html.unescape(value)
    try:
        return bytes(value, "utf-8").decode("unicode_escape", errors="replace")
    except Exception:
        return value


def _extract_envelope_id_from_text(*texts: Any) -> str:
    """从多个文本中提取 envelope_request_id"""
    for text in texts:
        if not text:
            continue
        decoded = unquote(str(text))
        match = re.search(r"envelope_request_id[:=]([0-9a-fA-F]+)", decoded)
        if match:
            return match.group(1)
    return ""


class GoPayCharger:
    """GoPay 红包领取器"""

    def __init__(
        self,
        chatgpt_session: Any,
        gopay_cfg: dict,
        otp_provider: Callable[[], str],
        log: Callable[[str], None] = logger.info,
        proxy: Optional[str] = None,
    ):
        self.chatgpt_session = chatgpt_session
        self.gopay_cfg = gopay_cfg
        self.otp_provider = otp_provider
        self.log = log
        self.proxy = proxy

        self.country_code = str(gopay_cfg.get("country_code") or "62")
        self.phone = str(gopay_cfg.get("phone_number") or "")
        self.pin = str(gopay_cfg.get("pin") or "")
        self.access_token = str(gopay_cfg.get("gopay_access_token") or "")

        if proxy:
            normalized = proxy
            if proxy.startswith("socks5://"):
                normalized = "socks5h://" + proxy[len("socks5://"):]
            self.chatgpt_session.proxies = {"http": normalized, "https": normalized}

    def _festival_envelope_cfg(self) -> dict:
        """获取红包配置"""
        return self.gopay_cfg.get("festival_envelope") or {}

    def _festival_resolve_envelope_request_id(self, cfg: dict) -> str:
        """解析红包短链获取 envelope_request_id"""
        short_link = str(cfg.get("short_link") or "").strip()
        if not short_link:
            raise GoPayError("红包短链为空")

        self.log(f"[gopay-envelope] 解析短链: {short_link}")

        # 1. 先尝试直接从 URL 提取（长链接可能已包含 ID）
        envelope_id = _extract_envelope_id_from_text(short_link, unquote(short_link))
        if envelope_id:
            self.log(f"[gopay-envelope] 从 URL 直接提取到 envelope_id: {envelope_id}")
            return envelope_id

        # 2. 短链需要 GET 跟随重定向解析
        try:
            resp = self.chatgpt_session.get(
                short_link,
                allow_redirects=True,
                timeout=30,
            )
        except Exception as e:
            raise GoPayError(f"短链请求失败: {e}")

        if resp.status_code >= 400:
            raise GoPayError(f"短链返回错误: {resp.status_code}")

        final_url = str(resp.url)
        page = resp.text
        self.log(f"[gopay-envelope] 最终 URL: {final_url}")

        # 3. 从页面提取 app_link (gopay:// 深链接)
        app_link = _extract_js_var(page, "app_link")
        store_link = _extract_js_var(page, "store_link")

        # 如果没找到 JS 变量，尝试直接搜索 gopay:// 链接
        if not app_link:
            match = re.search(r'(gopay://[^\'\"<>\s]+)', page)
            if match:
                app_link = match.group(1)

        # 4. 从多个来源提取 envelope_request_id
        envelope_id = _extract_envelope_id_from_text(
            app_link,
            unquote(app_link),
            store_link,
            page,
        )

        if not envelope_id:
            raise GoPayError(f"无法从短链解析 envelope_request_id (status={resp.status_code})")

        self.log(f"[gopay-envelope] 解析成功 envelope_id={envelope_id}")
        return envelope_id

    def _run_link_festival_envelope(self) -> dict:
        """领取红包"""
        cfg = self._festival_envelope_cfg()
        envelope_id = self._festival_resolve_envelope_request_id(cfg)

        if not self.access_token:
            raise GoPayError("缺少 access_token")

        self.log(f"[gopay-envelope] 领取红包 envelope_id={envelope_id}")

        # 构造领取请求
        url = "https://customer.gopayapi.com/api/v1/festival-envelopes/claim"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "User-Agent": "GoPay/2.8.0 (com.gojek.gopay; build:280; Android, 12)",
            "x-appid": "com.gojek.gopay",
            "x-apptype": "GOPAY",
            "x-platform": "Android",
        }

        body = {
            "envelope_request_id": envelope_id,
        }

        try:
            resp = self.chatgpt_session.post(
                url,
                json=body,
                headers=headers,
                timeout=30,
            )
        except Exception as e:
            raise GoPayError(f"领取请求失败: {e}")

        if resp.status_code >= 500:
            raise GoPayError(f"服务端错误: {resp.status_code}")

        try:
            data = resp.json()
        except Exception:
            raise GoPayError(f"响应解析失败: {resp.text[:200]}")

        # 解析结果
        if resp.status_code == 200:
            # 成功领取
            amount = data.get("amount", 0)
            currency = data.get("currency", "IDR")
            remaining = data.get("remaining", 0)
            total = data.get("total", 0)

            self.log(f"[gopay-envelope] 领取成功: {amount} {currency}")

            return {
                "ok": True,
                "amount": amount,
                "currency": currency,
                "remaining": remaining,
                "total": total,
            }

        # 领取失败
        error_code = data.get("error_code", "")
        error_message = data.get("error_message", "") or data.get("message", "")

        if "already claimed" in error_message.lower():
            reason = "已领取过"
        elif "expired" in error_message.lower():
            reason = "红包已过期"
        elif "exhausted" in error_message.lower():
            reason = "红包已领完"
        else:
            reason = error_message or error_code or "未知错误"

        self.log(f"[gopay-envelope] 领取失败: {reason}")

        return {
            "ok": False,
            "error": error_code,
            "reason": reason,
        }
