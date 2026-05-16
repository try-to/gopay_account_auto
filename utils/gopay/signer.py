"""GoPay App HMAC 签名模块

从 GoPay Android 客户端逆向得到的签名算法，用于构造合法的 GoPay API 请求。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import string
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit

import requests

try:
    from curl_cffi.requests import Session as _CurlCffiSession
except ImportError:
    _CurlCffiSession = None

HMAC_KEY = "4&G6DbV&j8QZs~{)(Ila_w_|v@aqJq]E-;*(J9PanZ8sm01kTi{X<iG``]d7P&L"
DEFAULT_X_E2 = "ED9A2B38749FBDE9ACA61D6A685B7"

BASE_URL = "https://accounts.goto-products.com"
API_URL = "https://api.gojekapi.com"
CUSTOMER_URL = "https://customer.gopayapi.com"
CLIENT_ID = "gopay:consumer:app"
CLIENT_SECRET = "raOUumeMRBNifqvZRFjvsgTnjAlaA9"
SIGNUP_BASIC_AUTH = "Basic YmI2NDg0MTMtYjYzNy00NDNhLThlYmYtMTc2Y2Y5YjVkYzMy"

DEFAULT_APP_VERSION = "2.8.0"

_PHONE_MAKES = ["HONOR", "Samsung", "Xiaomi", "OPPO", "vivo", "Realme", "Huawei", "OnePlus"]
_PHONE_MODELS = [
    ("HONOR", "BVL-AN20"), ("Samsung", "SM-A546B"), ("Samsung", "SM-G991B"),
    ("Xiaomi", "M2101K6G"), ("Xiaomi", "2201116SG"), ("OPPO", "CPH2399"),
    ("vivo", "V2145"), ("Realme", "RMX3393"), ("Huawei", "NOH-AN00"),
    ("OnePlus", "LE2115"),
]
_SCREEN_RESOLUTIONS = ["1080x2400", "1080x2340", "1440x2560", "1080x1920", "1080x2412"]
_ANDROID_VERSIONS = ["Android, 11", "Android, 12", "Android, 13", "Android, 14"]
_WIFI_PREFIXES = ["TP-Link", "Belkin", "ASUS", "Netgear", "Linksys", "Xiaomi", "Huawei"]


def _random_mac() -> str:
    return ":".join(f"{random.randint(0, 255):02x}" for _ in range(6))


def _random_wifi_ssid() -> str:
    prefix = random.choice(_WIFI_PREFIXES)
    suffix = "".join(random.choices(string.hexdigits[:16], k=12))
    return f"{prefix}_{suffix}"


def _random_x_m1() -> str:
    ts = int(time.time() * 1000)
    rand_id = random.randint(1000000000000000000, 9999999999999999999)
    make, model = random.choice(_PHONE_MODELS)
    mac = _random_mac()
    wifi = _random_wifi_ssid()
    resolution = random.choice(_SCREEN_RESOLUTIONS)
    conn_id = random.randint(100000, 999999)
    fingerprint_hash = hashlib.sha256(os.urandom(32)).digest()
    import base64
    fp_b64 = base64.b64encode(fingerprint_hash).decode().rstrip("=")
    device_hash = os.urandom(16).hex()
    device_uuid = str(uuid.uuid4())
    return (
        f"3:{ts}-{rand_id},4:{conn_id},5:{make}|3200|2,"
        f"6:{mac},7:{wifi},8:{resolution},"
        f"9:passive,network,fused,gps,10:1,"
        f"11:{fp_b64},"
        f"15:{device_hash},16:{device_uuid}"
    )


def _random_unique_id() -> str:
    return os.urandom(8).hex()


def sign_x_e1(headers: dict, method: str, host: str, path: str, body_text: str = "") -> str:
    """生成 x-e1 签名"""
    ts = int(time.time() * 1000)
    nonce = os.urandom(80).hex()
    h = {k.lower(): v for k, v in headers.items()}
    auth = h.get("authorization", "")
    bearer = auth[len("Bearer "):] if auth.startswith("Bearer ") else auth
    body_md5 = hashlib.md5(body_text.encode()).hexdigest()
    canonical = (
        f"{h.get('x-apptype', '')};"
        f"{h.get('x-phonemodel', '')}:{bearer};"
        f"{h.get('x-uniqueid', '')}:;"
        f"{body_md5}:{host}{path};"
        f"{method.upper()}:{ts};"
        f"{h.get('x-deviceos', '')}:{h.get('x-appversion', '')};"
        f"{h.get('x-m1', '')}:{h.get('x-appid', '')};"
        f"{nonce}:{h.get('x-phonemake', '')};"
        f"{h.get('x-platform', '')}"
    )
    digest = hmac.new(HMAC_KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return f"{digest}:{nonce}:D:{ts}"


def build_gopay_app_headers(
    authorization: str = "Bearer ",
    gopay_cfg: Optional[dict] = None,
) -> dict[str, str]:
    """构建 GoPay App 请求头"""
    cfg = gopay_cfg if gopay_cfg is not None else {}
    app_version = str(cfg.get("app_version") or DEFAULT_APP_VERSION)

    if not cfg.get("_device_fingerprint_initialized"):
        make, model = random.choice(_PHONE_MODELS)
        cfg.setdefault("_fp_device_os", random.choice(_ANDROID_VERSIONS))
        cfg.setdefault("_fp_phone_make", make)
        cfg.setdefault("_fp_phone_model", f"{make}, {model}")
        cfg.setdefault("_fp_unique_id", _random_unique_id())
        cfg.setdefault("_fp_x_m1", _random_x_m1())
        cfg.setdefault("_fp_transaction_id", str(uuid.uuid4()))
        lat = round(-6.2 + random.uniform(-0.05, 0.05), 7)
        lng = round(106.8 + random.uniform(-0.05, 0.05), 7)
        cfg.setdefault("_fp_location", f"{lat},{lng}")
        cfg["_device_fingerprint_initialized"] = True

    device_os = str(cfg.get("device_os") or cfg["_fp_device_os"])
    phone_make = str(cfg.get("phone_make") or cfg["_fp_phone_make"])
    phone_model = str(cfg.get("phone_model") or cfg["_fp_phone_model"])
    unique_id = str(cfg.get("unique_id") or cfg["_fp_unique_id"])
    x_m1 = str(cfg.get("x_m1") or cfg["_fp_x_m1"])
    location = str(cfg.get("x_location") or cfg["_fp_location"])

    return {
        "accept-encoding": "gzip",
        "country-code": "ID",
        "gojek-country-code": "ID",
        "gojek-service-area": "1",
        "x-appversion": app_version,
        "x-help-version": app_version,
        "x-location": location,
        "x-location-accuracy": f"0.0{random.randint(10, 99)}999999552965164",
        "x-uniqueid": unique_id,
        "x-phonemake": phone_make,
        "x-phonemodel": phone_model,
        "x-deviceos": device_os,
        "x-user-type": "customer",
        "x-appid": "com.gojek.gopay",
        "gojek-timezone": "Asia/Jakarta",
        "x-apptype": "GOPAY",
        "x-user-locale": "en_ID",
        "accept-language": "en-ID",
        "x-platform": "Android",
        "user-agent": f"GoPay/{app_version} (com.gojek.gopay; build:{app_version.replace('.', '')}; {device_os})",
        "content-type": "application/json",
        "x-m1": x_m1,
        "x-e2": DEFAULT_X_E2,
        "x-authsdk-version": "1.0.0",
        "x-cvsdk-version": "1.0.0",
        "authorization": authorization,
        "x-request-id": str(uuid.uuid1()),
        "transaction-id": str(cfg.get("_fp_transaction_id") or uuid.uuid4()),
    }


def signed_post(
    url: str,
    body: Any,
    authorization: str = "Bearer ",
    gopay_cfg: Optional[dict] = None,
    keep_auth: bool = False,
    extra_headers: Optional[dict] = None,
    session: Optional[Any] = None,
    timeout: int = 30,
) -> requests.Response:
    """发送带签名的 POST 请求"""
    parsed = urlsplit(url)
    host = parsed.netloc
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    headers = build_gopay_app_headers(authorization, gopay_cfg)
    if extra_headers:
        headers.update(extra_headers)
    headers["host"] = host
    headers["x-e1"] = sign_x_e1(headers, "POST", host, path, body_text)
    if not keep_auth:
        headers.pop("authorization", None)
    s = session or requests
    return s.post(url, data=body_text, headers=headers, timeout=timeout)


def create_gopay_session(proxy: Optional[str] = None) -> Any:
    """创建 GoPay App API 专用 session（优先使用 curl_cffi）"""
    if _CurlCffiSession is not None:
        s = _CurlCffiSession(impersonate="chrome136")
    else:
        s = requests.Session()
    try:
        s.trust_env = False
    except Exception:
        pass
    if proxy:
        normalized = proxy
        if proxy.startswith("socks5://"):
            normalized = "socks5h://" + proxy[len("socks5://"):]
        s.proxies = {"http": normalized, "https": normalized}
    else:
        s.proxies = {"http": "", "https": ""}
    return s
