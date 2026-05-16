"""GoPay 钱包账号管理 — 纯 HTTP 登录/注册

通过 GoPay App 级 HMAC 签名 API 实现：
- auto_login: 已有账号通过 SMS OTP 登录获取 access_token
- auto_signup: 新号注册 GoPay 钱包并设置 PIN
"""
from __future__ import annotations

import json
import logging
import random
import re
import string
import time
import uuid
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .signer import (
    API_URL,
    BASE_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    CUSTOMER_URL,
    SIGNUP_BASIC_AUTH,
    build_gopay_app_headers,
    create_gopay_session,
    sign_x_e1,
    signed_post,
)

logger = logging.getLogger(__name__)

DEFAULT_PIN = "123456"


class GoPayAccountError(RuntimeError):
    pass


def _safe_json(resp: Any) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class GoPayAccountResult:
    __slots__ = ("access_token", "refresh_token", "account_id", "phone", "country_code", "pin", "session")

    def __init__(self):
        self.access_token: str = ""
        self.refresh_token: str = ""
        self.account_id: str = ""
        self.phone: str = ""
        self.country_code: str = ""
        self.pin: str = ""
        self.session = None


def _random_name() -> str:
    return "".join(random.choices(string.ascii_uppercase, k=3))


def _extract_errors(resp_json: dict) -> list[dict]:
    return resp_json.get("errors", []) if isinstance(resp_json, dict) else []


def _has_error_code(resp_json: dict, code: str) -> bool:
    for err in _extract_errors(resp_json):
        if isinstance(err, dict) and err.get("code") == code:
            return True
    return False


def auto_login(
    phone: str,
    country_code: str = "+62",
    pin: str = DEFAULT_PIN,
    otp_provider: Callable[[str], str] = lambda label: "",
    gopay_cfg: Optional[dict] = None,
    proxy: Optional[str] = None,
    log: Callable[[str], None] = logger.info,
) -> GoPayAccountResult:
    """通过 SMS OTP 登录已有 GoPay 账号"""
    phone = re.sub(r"\D", "", phone)
    if not country_code.startswith("+"):
        country_code = f"+{country_code}"
    session = create_gopay_session(proxy)
    cfg = gopay_cfg if gopay_cfg is not None else {}

    log(f"[gopay-login] 探测 {country_code}{phone}")
    resp = signed_post(
        f"{BASE_URL}/goto-auth/login/methods",
        {
            "phone_number": phone,
            "country_code": country_code,
            "email": "",
            "device_verification_token_id": "",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        gopay_cfg=cfg, session=session,
    )
    if resp.status_code >= 500:
        raise GoPayAccountError(f"login/methods 服务端错误: {resp.status_code}")
    data = _safe_json(resp) if resp.status_code < 400 else {}
    if _has_error_code(data, "auth:error:user:not_found"):
        raise GoPayAccountError(f"账号不存在: {country_code}{phone}")

    time.sleep(random.uniform(1.5, 3.0))
    verification_id = str(uuid.uuid4())
    resp = signed_post(
        f"{BASE_URL}/cvs/v1/methods",
        {
            "country_code": country_code,
            "email_address": None,
            "client_id": CLIENT_ID,
            "phone_number": phone,
            "client_secret": CLIENT_SECRET,
            "flow": "login_1fa",
            "device_verification_token_id": None,
        },
        gopay_cfg=cfg, session=session,
    )
    methods_data = _safe_json(resp).get("data", {}) if resp.status_code < 500 else {}
    verification_id = str(methods_data.get("verification_id") or verification_id)

    time.sleep(random.uniform(1.0, 2.5))
    resp = signed_post(
        f"{BASE_URL}/cvs/v1/initiate",
        {
            "verification_id": verification_id,
            "flow": "login_1fa",
            "verification_method": "otp_sms",
            "country_code": country_code,
            "email_address": None,
            "client_id": CLIENT_ID,
            "phone_number": phone,
            "client_secret": CLIENT_SECRET,
            "is_multiple_method": None,
            "device_verification_token_id": None,
        },
        gopay_cfg=cfg, session=session,
    )
    log(f"[gopay-login] initiate resp={resp.status_code}")
    init_data = _safe_json(resp).get("data", {}) if resp.status_code < 500 else {}
    otp_token = init_data.get("otp_token", "")
    if not otp_token:
        raise GoPayAccountError(f"login initiate 未返回 otp_token: {resp.text[:300]}")

    log("[gopay-login] 等待 SMS OTP...")
    otp = otp_provider("gopay_login")
    if not otp:
        raise GoPayAccountError("OTP 未提供")

    resp = signed_post(
        f"{BASE_URL}/cvs/v1/verify",
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "flow": "login_1fa",
            "verification_method": "otp_sms",
            "verification_id": verification_id,
            "data": {"otp": otp, "otp_token": otp_token},
        },
        gopay_cfg=cfg, session=session,
    )
    verify_data = _safe_json(resp).get("data", {}) if resp.status_code < 500 else {}
    verification_token = verify_data.get("verification_token", "")
    if not verification_token:
        raise GoPayAccountError(f"login verify 失败")

    resp = signed_post(
        f"{BASE_URL}/goto-auth/accountlist",
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        extra_headers={"verification-token": f"Bearer {verification_token}"},
        gopay_cfg=cfg, session=session,
    )
    acc_data = _safe_json(resp).get("data", {}) if resp.status_code < 500 else {}
    account_list = acc_data.get("account_list", []) if isinstance(acc_data, dict) else []
    one_fa_token = str(acc_data.get("1fa_token") or "") if isinstance(acc_data, dict) else ""
    account_id = ""
    if isinstance(account_list, list) and account_list:
        first = account_list[0] if isinstance(account_list[0], dict) else {}
        account_id = str(first.get("account_id") or first.get("id") or "")
    if not account_id:
        account_id = str(acc_data.get("account_id") or acc_data.get("id") or "")
    if not account_id or not one_fa_token:
        raise GoPayAccountError(f"accountlist 缺少 account_id/1fa_token")

    resp = signed_post(
        f"{BASE_URL}/goto-auth/token",
        {
            "grant_type": "cvs",
            "account_id": account_id,
            "token": one_fa_token,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "ext_user_token": None,
        },
        gopay_cfg=cfg, session=session,
    )
    token_data = _safe_json(resp).get("data", {}) if resp.status_code < 500 else {}
    access_token = str(token_data.get("access_token") or "") if isinstance(token_data, dict) else ""
    refresh_token = str(token_data.get("refresh_token") or "") if isinstance(token_data, dict) else ""
    if not access_token:
        raise GoPayAccountError(f"token 换取失败")

    log(f"[gopay-login] 登录成功")
    result = GoPayAccountResult()
    result.access_token = access_token
    result.refresh_token = refresh_token
    result.account_id = account_id
    result.phone = phone
    result.country_code = country_code
    result.pin = pin
    result.session = session
    return result


def auto_signup(
    phone: str,
    country_code: str = "+62",
    pin: str = DEFAULT_PIN,
    otp_provider: Callable[[str], str] = lambda label: "",
    gopay_cfg: Optional[dict] = None,
    proxy: Optional[str] = None,
    log: Callable[[str], None] = logger.info,
    pre_pin_otp_hook: Callable[[], None] | None = None,
) -> GoPayAccountResult:
    """注册新 GoPay 钱包并设置 PIN（需要两次 OTP）"""
    phone = re.sub(r"\D", "", phone)
    if not country_code.startswith("+"):
        country_code = f"+{country_code}"
    session = create_gopay_session(proxy)
    cfg = gopay_cfg if gopay_cfg is not None else {}
    name = _random_name()

    log(f"[gopay-signup] 探测 {country_code}{phone}")
    resp = signed_post(
        f"{BASE_URL}/goto-auth/login/methods",
        {
            "phone_number": phone,
            "country_code": country_code,
            "email": "",
            "device_verification_token_id": "",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
        gopay_cfg=cfg, session=session,
    )
    data = _safe_json(resp) if resp.status_code < 500 else {}
    if not _has_error_code(data, "auth:error:user:not_found"):
        raise GoPayAccountError(f"号码已注册或探测异常")

    time.sleep(random.uniform(1.5, 3.0))
    log("[gopay-signup] 触发注册 SMS OTP")
    verification_id = str(uuid.uuid4())
    resp = signed_post(
        f"{BASE_URL}/cvs/v1/methods",
        {
            "country_code": country_code,
            "email_address": None,
            "client_id": CLIENT_ID,
            "phone_number": phone,
            "client_secret": CLIENT_SECRET,
            "flow": "signup",
            "device_verification_token_id": None,
        },
        gopay_cfg=cfg, session=session,
    )
    methods_data = _safe_json(resp).get("data", {}) if resp.status_code < 500 else {}
    verification_id = str(methods_data.get("verification_id") or verification_id)
    log(f"[gopay-signup] methods resp={resp.status_code}, verification_id={verification_id[:8]}")

    time.sleep(random.uniform(1.0, 2.5))
    resp = signed_post(
        f"{BASE_URL}/cvs/v1/initiate",
        {
            "verification_id": verification_id,
            "flow": "signup",
            "verification_method": "otp_sms",
            "country_code": country_code,
            "email_address": None,
            "client_id": CLIENT_ID,
            "phone_number": phone,
            "client_secret": CLIENT_SECRET,
            "is_multiple_method": None,
            "device_verification_token_id": None,
        },
        gopay_cfg=cfg, session=session,
    )
    init_data = _safe_json(resp).get("data", {}) if resp.status_code < 500 else {}
    otp_token = init_data.get("otp_token", "")
    if not otp_token:
        raise GoPayAccountError(f"signup initiate 未返回 otp_token: {resp.text[:300]}")

    log(f"[gopay-signup] 等待注册 SMS OTP...")
    otp = otp_provider("gopay_signup")
    if not otp:
        raise GoPayAccountError("注册 OTP 未提供")

    resp = signed_post(
        f"{BASE_URL}/cvs/v1/verify",
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "flow": "signup",
            "verification_method": "otp_sms",
            "verification_id": verification_id,
            "data": {"otp": otp, "otp_token": otp_token},
        },
        gopay_cfg=cfg, session=session,
    )
    verify_data = _safe_json(resp).get("data", {}) if resp.status_code < 500 else {}
    verification_token = verify_data.get("verification_token", "")
    if not verification_token:
        raise GoPayAccountError(f"signup verify 失败")

    log(f"[gopay-signup] 创建账号 (name={name})")
    resp = signed_post(
        f"{API_URL}/v7/customers/signup",
        {
            "client_name": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "data": {
                "name": name,
                "phone": f"{country_code}{phone}",
                "email": "",
                "signed_up_country": country_code,
                "onboarding_partner": "gopay_consumer_app",
            },
        },
        authorization=SIGNUP_BASIC_AUTH,
        keep_auth=True,
        extra_headers={"verification-token": f"Bearer {verification_token}"},
        gopay_cfg=cfg, session=session,
    )
    signup_data = _safe_json(resp).get("data") or {} if resp.status_code < 500 else {}
    signup_access = str(signup_data.get("access_token") or "") if isinstance(signup_data, dict) else ""
    signup_refresh = str(signup_data.get("refresh_token") or "") if isinstance(signup_data, dict) else ""
    account_id = str(
        signup_data.get("resource_owner_id")
        or (signup_data.get("customer") or {}).get("id")
        or ""
    ) if isinstance(signup_data, dict) else ""
    if not signup_access:
        raise GoPayAccountError(f"signup 失败")

    log("[gopay-signup] 换取正式 token")
    resp = signed_post(
        f"{BASE_URL}/goto-auth/token",
        {
            "grant_type": "refresh_token",
            "account_id": account_id,
            "token": signup_refresh,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": signup_refresh,
            "ext_user_token": signup_access,
        },
        authorization=f"Bearer {signup_access}",
        keep_auth=True,
        gopay_cfg=cfg, session=session,
    )
    token_data = _safe_json(resp).get("data") or {} if resp.status_code < 500 else {}
    access_token = str(token_data.get("access_token") or "") if isinstance(token_data, dict) else ""
    refresh_token = str(token_data.get("refresh_token") or "") if isinstance(token_data, dict) else ""
    if not access_token:
        access_token = signup_access
        refresh_token = signup_refresh

    log(f"[gopay-signup] 设置 PIN={pin}")
    _setup_pin(
        access_token=access_token,
        pin=pin,
        otp_provider=otp_provider,
        gopay_cfg=cfg,
        session=session,
        log=log,
        pre_otp_hook=pre_pin_otp_hook,
    )

    log(f"[gopay-signup] 注册完成")
    result = GoPayAccountResult()
    result.access_token = access_token
    result.refresh_token = refresh_token
    result.account_id = account_id
    result.phone = phone
    result.country_code = country_code
    result.pin = pin
    result.session = session
    return result


def _setup_pin(
    access_token: str,
    pin: str,
    otp_provider: Callable[[str], str],
    gopay_cfg: dict,
    session: Any,
    log: Callable[[str], None],
    pre_otp_hook: Callable[[], None] | None = None,
) -> None:
    """设置 GoPay PIN（需要一次 SMS OTP 验证）"""
    # 7a: methods
    resp = signed_post(
        f"{BASE_URL}/cvs/v1/methods",
        {
            "country_code": None,
            "email_address": None,
            "client_id": CLIENT_ID,
            "phone_number": None,
            "client_secret": CLIENT_SECRET,
            "flow": "goto_pin_wa_sms",
            "device_verification_token_id": None,
        },
        authorization=f"Bearer {access_token}",
        keep_auth=True,
        gopay_cfg=gopay_cfg,
        session=session,
    )
    methods_data = _safe_json(resp).get("data", {}) if resp.status_code < 500 else {}
    pin_verification_id = methods_data.get("verification_id", "")
    if not pin_verification_id:
        raise GoPayAccountError(f"PIN methods 失败: {resp.text[:300]}")

    # 7b: initiate 前先 reactivate SMS
    if pre_otp_hook:
        pre_otp_hook()

    time.sleep(random.uniform(1.0, 2.5))

    # 7b: initiate
    resp = signed_post(
        f"{BASE_URL}/cvs/v1/initiate",
        {
            "verification_id": pin_verification_id,
            "flow": "goto_pin_wa_sms",
            "verification_method": "otp_sms",
            "country_code": None,
            "email_address": None,
            "client_id": CLIENT_ID,
            "phone_number": None,
            "client_secret": CLIENT_SECRET,
            "is_multiple_method": None,
            "device_verification_token_id": None,
        },
        authorization=f"Bearer {access_token}",
        keep_auth=True,
        gopay_cfg=gopay_cfg,
        session=session,
    )
    init_data = _safe_json(resp).get("data", {}) if resp.status_code < 500 else {}
    pin_otp_token = init_data.get("otp_token", "")
    if not pin_otp_token:
        raise GoPayAccountError(f"PIN initiate 失败: {resp.text[:300]}")

    # 7c: 等待 PIN OTP（超时重发，最多 3 次）
    pin_otp = ""
    _pin_otp_waits = [30, 60, 120]
    for pin_otp_attempt in range(1, 4):
        log(f"[gopay-signup] 等待 PIN 设置 SMS OTP (尝试 {pin_otp_attempt}/3)...")
        pin_otp = otp_provider("gopay_pin_setup")
        if pin_otp:
            break
        if pin_otp_attempt < 3:
            wait_s = _pin_otp_waits[pin_otp_attempt - 1]
            log(f"[gopay-signup] PIN OTP 超时，{wait_s}s 后重新触发 initiate...")
            time.sleep(wait_s)
            resp = signed_post(
                f"{BASE_URL}/cvs/v1/initiate",
                {
                    "verification_id": pin_verification_id,
                    "flow": "goto_pin_wa_sms",
                    "verification_method": "otp_sms",
                    "country_code": None,
                    "email_address": None,
                    "client_id": CLIENT_ID,
                    "phone_number": None,
                    "client_secret": CLIENT_SECRET,
                    "is_multiple_method": None,
                    "device_verification_token_id": None,
                },
                authorization=f"Bearer {access_token}",
                keep_auth=True,
                gopay_cfg=gopay_cfg,
                session=session,
            )
            new_data = _safe_json(resp).get("data", {}) if resp.status_code < 500 else {}
            new_token = new_data.get("otp_token", "")
            if new_token:
                pin_otp_token = new_token
    if not pin_otp:
        raise GoPayAccountError("PIN OTP 未提供")

    # 7d: verify
    resp = signed_post(
        f"{BASE_URL}/cvs/v1/verify",
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "flow": "goto_pin_wa_sms",
            "verification_method": "otp_sms",
            "verification_id": pin_verification_id,
            "data": {"otp": pin_otp, "otp_token": pin_otp_token},
        },
        authorization=f"Bearer {access_token}",
        keep_auth=True,
        gopay_cfg=gopay_cfg,
        session=session,
    )
    verify_data = _safe_json(resp).get("data", {}) if resp.status_code < 500 else {}
    pin_verification_token = verify_data.get("verification_token", "")
    if not pin_verification_token:
        raise GoPayAccountError(f"PIN verify 失败: {resp.text[:300]}")

    # 7e: setup PIN
    headers = build_gopay_app_headers(f"Bearer {access_token}", gopay_cfg)
    headers["verification-token"] = f"Bearer {pin_verification_token}"
    headers["is-token-required"] = "false"
    headers["host"] = urlsplit(CUSTOMER_URL).netloc

    pin_body = {"client_id": "", "pin": pin, "challenge_id": ""}
    body_text = json.dumps(pin_body, separators=(",", ":"))
    path = "/api/v2/users/pins/setup/tokens"
    headers["x-e1"] = sign_x_e1(headers, "POST", headers["host"], path, body_text)
    headers.pop("host", None)

    resp = session.post(
        f"{CUSTOMER_URL}{path}",
        data=body_text,
        headers=headers,
        timeout=30,
    )
    if resp.status_code >= 400:
        raise GoPayAccountError(f"PIN setup 失败 ({resp.status_code}): {resp.text[:300]}")
    log(f"[gopay-signup] PIN 设置成功")
