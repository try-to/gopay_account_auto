"""GoPay Account Auto - 主流程

自动化 GoPay 钱包注册/登录 + 红包领取的完整流程。
"""

import sys
import json
import time
import random
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

import yaml
from utils.gopay.account import auto_signup, auto_login, GoPayAccountError
from utils.gopay.charger import GoPayCharger, GoPayError
from utils.hero_sms import get_number, SmsActivation, set_status, STATUS_RESEND

# ═══════════════════════════════════════════════════════════════
# 配置加载
# ═══════════════════════════════════════════════════════════════

config_path = ROOT_DIR / "config.yaml"
if not config_path.exists():
    print("[错误] 配置文件不存在，请复制 config.example.yaml 为 config.yaml 并填写配置")
    sys.exit(1)

with open(config_path, "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

gopay_section = cfg.get("account_pay", {}).get("GoPay") or {}
festival_cfg = gopay_section.get("festival_envelope") or {}
hero_cfg = cfg.get("hero_sms", {}) or {}
signup_cfg = gopay_section.get("signup", {}) or {}

# 从配置读取 hero-sms 参数
hero_api_key = str(hero_cfg.get("api_key") or "")
hero_base_url = str(hero_cfg.get("base_url") or "https://api.hero-sms.com")

# 代理配置
proxy = str(cfg.get("proxy") or "http://127.0.0.1:7890")

print(f"使用代理: {proxy}")
print(f"红包配置: enabled={festival_cfg.get('enabled')}")
print(f"hero-sms: enabled={hero_cfg.get('enabled')}")

# 参数校验
if not festival_cfg.get("enabled") or not str(festival_cfg.get("short_link") or "").strip():
    print("\n[跳过] 红包未启用或 short_link 为空")
    sys.exit(0)

if not hero_cfg.get("enabled"):
    print("\n[跳过] hero-sms 未启用，无法自动取号")
    sys.exit(0)

signup_pin = signup_cfg.get("default_pin") or "123456"
country_code = signup_cfg.get("country_code") or "+62"
sms_service = hero_cfg.get("service") or "ni"
sms_country = int(hero_cfg.get("country") or 6)
sms_timeout = int(hero_cfg.get("poll_timeout_sec") or 300)

# ═══════════════════════════════════════════════════════════════
# Step 1: hero-sms 取号
# ═══════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("Step 1: hero-sms 取号")
print("=" * 60)

print(f"[1/5] hero-sms 取号 (service={sms_service}, country={sms_country})...")
activation_id, phone_raw, err = get_number(
    service_code=sms_service,
    country_id=sms_country,
    base_url=hero_base_url,
    api_key=hero_api_key,
    log=print,
)

if not activation_id:
    print(f"[失败] 取号失败: {err}")
    sys.exit(1)

phone = phone_raw.lstrip("+")
if phone.startswith("62"):
    phone = phone[2:]

print(f"[成功] 取到号码: +62{phone} (activation_id={activation_id})")

# 使用 SmsActivation 管理验证码生命周期
sms_activation = SmsActivation(
    activation_id=activation_id,
    phone=phone_raw,
    country_id=sms_country,
    base_url=hero_base_url,
    api_key=hero_api_key,
    log=print,
)

# ═══════════════════════════════════════════════════════════════
# Step 2: 注册/登录钱包
# ═══════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("Step 2: 注册/登录 GoPay 钱包")
print("=" * 60)

otp_call_count = [0]


def auto_otp_provider(label: str) -> str:
    """自动获取 SMS 验证码"""
    otp_call_count[0] += 1
    call_n = otp_call_count[0]
    print(f"  [{label}] 等待第 {call_n} 次 SMS 验证码 (timeout={sms_timeout}s)...")
    code = sms_activation.wait_code(timeout_sec=sms_timeout, label=f"{label}#{call_n}")
    if code:
        print(f"  [{label}] 收到验证码: {code}")
        return code
    print(f"  [{label}] 等码超时")
    return ""


print(f"[2/5] 注册/登录 +62{phone} (PIN={signup_pin})...")
access_token = ""
result_phone = ""
result_pin = ""
gopay_session = None

# 共享 gopay_cfg 确保设备指纹一致
shared_gopay_cfg = {}


def reactivate_before_pin():
    """initiate 前先 reactivate SMS，确保 hero-sms 准备好接收 PIN 验证码"""
    set_status(hero_base_url, hero_api_key, activation_id, STATUS_RESEND)
    print("  [pre-pin] reactivate SMS (status=3)")


try:
    signup_result = auto_signup(
        phone=phone,
        country_code=country_code,
        pin=signup_pin,
        otp_provider=auto_otp_provider,
        gopay_cfg=shared_gopay_cfg,
        proxy=proxy,
        log=print,
        pre_pin_otp_hook=reactivate_before_pin,
    )
    print("[3/5] 注册成功!")
    access_token = signup_result.access_token
    result_phone = signup_result.phone
    result_pin = signup_result.pin
    gopay_session = signup_result.session

except GoPayAccountError as e:
    if "已注册" in str(e) or "探测异常" in str(e):
        print(f"       号码已注册，等待 2s 后切换 auto_login...")
        time.sleep(2)
        login_result = auto_login(
            phone=phone,
            country_code=country_code,
            pin=signup_pin,
            otp_provider=auto_otp_provider,
            gopay_cfg=shared_gopay_cfg,
            proxy=proxy,
            log=print,
        )
        print("[3/5] 登录成功!")
        access_token = login_result.access_token
        result_phone = login_result.phone
        result_pin = login_result.pin
        gopay_session = login_result.session
    else:
        print(f"\n[失败] 注册失败: {e}")
        sms_activation.cancel_activation()
        sys.exit(1)

except Exception as e:
    print(f"\n[失败] 未知异常: {type(e).__name__}: {e}")
    sms_activation.cancel_activation()
    sys.exit(1)

if not access_token:
    print("\n[跳过] 未获取到 access_token，跳过红包流程")
    sms_activation.cancel_activation()
    sys.exit(1)

print(f"       phone: +62{result_phone}")
print(f"       pin: {result_pin}")
print(f"       access_token: {access_token[:30]}...")
sms_activation.release()

# ═══════════════════════════════════════════════════════════════
# Step 3: 领取红包
# ═══════════════════════════════════════════════════════════════

print("\n" + "=" * 60)
print("Step 3: 领取红包")
print("=" * 60)

import requests

gopay_cfg_for_charger = {
    "country_code": "62",
    "phone_number": result_phone,
    "pin": result_pin,
    "gopay_access_token": access_token,
    "festival_envelope": festival_cfg,
}

# 继承注册时的设备指纹
for k, v in shared_gopay_cfg.items():
    if k.startswith("_fp_") or k == "_device_fingerprint_initialized":
        gopay_cfg_for_charger[k] = v

# 复用注册/登录时的 session（保持 TLS 指纹 + cookie 一致）
session = gopay_session or requests.Session()
if not gopay_session:
    session.headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_2_1) AppleWebKit/537.36"
    )

charger = GoPayCharger(
    chatgpt_session=session,
    gopay_cfg=gopay_cfg_for_charger,
    otp_provider=lambda: "",
    log=print,
    proxy=proxy,
)

cfg_envelope = charger._festival_envelope_cfg()
try:
    envelope_id = charger._festival_resolve_envelope_request_id(cfg_envelope)
    print(f"       envelope_request_id: {envelope_id}")
except GoPayError as e:
    print(f"[失败] 短链解析失败: {e}")
    envelope_id = ""

if envelope_id:
    print(f"\n[4/5] 领取红包...")
    try:
        result = charger._run_link_festival_envelope()
        print(f"\n结果: {json.dumps(result, ensure_ascii=False, indent=2)}")
        if result.get("ok"):
            remaining = result.get("remaining")
            total = result.get("total")
            print(f"[成功] 红包领取成功! 剩余: {remaining}/{total}")
        else:
            print(f"[警告] 红包未成功: {result.get('error') or result.get('reason')}")
    except GoPayError as e:
        print(f"[失败] 红包流程异常: {e}")
    except Exception as e:
        print(f"[失败] 未知异常: {type(e).__name__}: {e}")
else:
    print("\n[跳过] envelope_id 为空，跳过领取")

print("\n" + "=" * 60)
print("流程完成")
print("=" * 60)
