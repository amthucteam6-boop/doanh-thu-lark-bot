"""
Gửi báo cáo doanh thu 3 brand (BPP/Waji/39Beef) vào nhóm Lark, chạy trên GitHub Actions
(không phụ thuộc máy Mac có bật hay không). Credentials lấy từ biến môi trường
(GitHub Actions Secrets) — không hard-code trong file này.
"""

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SLOT = os.environ.get("SLOT", "now")
SLOT_LABELS = {"trua": "Trưa (12h)", "chieu": "Chiều (15h)", "toi": "Tối (22h)", "now": "Cập nhật"}
SLOT_CUTOFFS = {"trua": "12:00:00", "chieu": "15:00:00", "toi": "22:00:00", "now": "23:59:59"}

CUKCUK_BASE = "https://graphapi.cukcuk.vn"
BRANDS_CUKCUK = {
    "bpp": {"domain": "beardpapa", "app_id": "CUKCUKOpenPlatform", "secret": os.environ["CUKCUK_BPP_SECRET"]},
    "waji": {"domain": "waji", "app_id": "CUKCUKOpenPlatform", "secret": os.environ["CUKCUK_WAJI_SECRET"]},
    "39beef": {"domain": "39beef", "app_id": "CUKCUKOpenPlatform", "secret": os.environ["CUKCUK_39BEEF_SECRET"]},
}
DISPLAY_UNITS = [
    ("bpp", "Beard Papa's", "bpp", None),
    ("waji", "Waji", "waji", None),
    ("39beef_th", "39Beef - 245 Tô Hiệu", "39beef", "245"),
    ("39beef_vph", "39Beef - 34 Vũ Phạm Hàm", "39beef", "34"),
]

LARK_APP_ID = os.environ["LARK_APP_ID"]
LARK_APP_SECRET = os.environ["LARK_APP_SECRET"]
LARK_CHAT_ID = os.environ["LARK_CHAT_ID"]
LARK_BASE = "https://open.larksuite.com"


def http_json(method, url, payload=None, headers=None, params=None, timeout=30, retries=3):
    """
    CukCuk thỉnh thoảng timeout tạm thời khi 1 brand có nhiều hoá đơn/trang
    (vd BPP ~500-600 bill/ngày, nhiều trang hơn hẳn Waji/39Beef) — thử lại vài
    lần trước khi báo lỗi hẳn, thay vì mất cả brand chỉ vì 1 request bị chậm.
    """
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    last_exc = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} for {url}: {body}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Timeout/network error for {url} sau {retries} lần thử: {last_exc}")


def cukcuk_login(cfg):
    login_time = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    msg = json.dumps({"AppID": cfg["app_id"], "Domain": cfg["domain"], "LoginTime": login_time}, separators=(",", ":"))
    sig = hmac.new(cfg["secret"].encode(), msg.encode(), hashlib.sha256).hexdigest()
    data = http_json("POST", f"{CUKCUK_BASE}/api/Account/Login", payload={
        "AppID": cfg["app_id"], "Domain": cfg["domain"], "LoginTime": login_time, "SignatureInfo": sig
    })
    if data.get("Code") != 200 or "Data" not in data:
        raise RuntimeError(f"CukCuk login failed for {cfg['domain']}: {data}")
    return data["Data"]


def cukcuk_headers(token):
    return {"Authorization": f"Bearer {token['AccessToken']}", "CompanyCode": token.get("CompanyCode", "")}


def get_branches(headers):
    data = http_json("GET", f"{CUKCUK_BASE}/api/v1/branchs/all", headers=headers)
    return [b for b in data.get("Data", []) if not b.get("Inactive") and not b.get("IsBaseDepot")]


def fetch_invoices(branch_id, date_iso, headers):
    since = date_iso + "T00:00:00"
    page, invoices = 1, []
    while True:
        r = http_json("POST", f"{CUKCUK_BASE}/api/v1/sainvoices/paging", headers=headers,
                       payload={"Page": page, "Limit": 100, "BranchId": branch_id, "LastSyncDate": since})
        data = r.get("Data", [])
        for inv in data:
            if inv.get("PaymentStatus") in (4, 5):
                continue
            if (inv.get("RefDate") or "")[:10] == date_iso:
                invoices.append(inv)
        if len(data) < 100:
            break
        last_date = (data[-1].get("RefDate") or "")[:10]
        if last_date and last_date < date_iso:
            break
        page += 1
    return invoices


def fetch_brand_slot(cukcuk_key, date_iso, cutoff):
    cfg = BRANDS_CUKCUK[cukcuk_key]
    token = cukcuk_login(cfg)
    headers = cukcuk_headers(token)
    branches = get_branches(headers)
    by_id = {b["Id"]: b["Name"] for b in branches}
    all_inv = []
    for b in branches:
        for inv in fetch_invoices(b["Id"], date_iso, headers):
            inv["BranchId"] = b["Id"]
            all_inv.append(inv)
    filtered = [inv for inv in all_inv if (inv.get("RefDate") or "")[11:19] <= cutoff]
    per_branch = {}
    for inv in filtered:
        bid = inv.get("BranchId")
        rev = float(inv.get("TotalAmount") or inv.get("Amount") or 0)
        d = per_branch.setdefault(bid, {"revenue": 0.0, "bills": 0})
        d["revenue"] += rev
        d["bills"] += 1
    return {by_id.get(bid, bid): agg for bid, agg in per_branch.items()}


def fmt_money(v):
    return f"{round(v):,.0f}".replace(",", ".") + " đ"


def fmt_k(v):
    return f"{round(v/1000):,.0f}".replace(",", ".") + "k"


def main():
    now_vn = datetime.now(ZoneInfo("Asia/Saigon"))
    now_hm = now_vn.strftime("%H:%M")

    # GitHub Actions cron có thể trễ nhiều giờ (đã thấy trễ >3 tiếng). Nếu 1 lần chạy
    # theo lịch (không phải lệnh "now") bị trễ qua nửa đêm, coi như vẫn thuộc về ngày
    # hôm trước — tránh báo nhầm 0đ cho ngày mới vừa bắt đầu vài phút.
    if SLOT != "now" and now_vn.hour < 6:
        today = (now_vn - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        today = now_vn.strftime("%Y-%m-%d")

    cutoff = SLOT_CUTOFFS[SLOT]
    header = f"Cập nhật lúc {now_hm}" if SLOT == "now" else SLOT_LABELS[SLOT]
    print("Ngày tính (Asia/Saigon):", today, "cutoff:", cutoff)

    cukcuk_cache, cukcuk_errors = {}, {}
    lines, failed = [], []
    grand_rev, grand_bills = 0.0, 0

    for unit_key, label, cukcuk_key, match in DISPLAY_UNITS:
        if cukcuk_key not in cukcuk_cache and cukcuk_key not in cukcuk_errors:
            try:
                cukcuk_cache[cukcuk_key] = fetch_brand_slot(cukcuk_key, today, cutoff)
            except Exception as exc:  # noqa: BLE001 - 1 brand lỗi không được làm hỏng cả báo cáo
                cukcuk_errors[cukcuk_key] = str(exc)
                print(f"LỖI lấy dữ liệu {cukcuk_key}: {exc}")

        if cukcuk_key in cukcuk_errors:
            failed.append((label, cukcuk_errors[cukcuk_key]))
            continue

        by_branch = cukcuk_cache[cukcuk_key]
        if match is None:
            rev = sum(v["revenue"] for v in by_branch.values())
            bills = sum(v["bills"] for v in by_branch.values())
        else:
            found = next((v for k, v in by_branch.items() if match in k), {"revenue": 0.0, "bills": 0})
            rev, bills = found["revenue"], found["bills"]

        aov = round(rev / bills) if bills else 0
        grand_rev += rev
        grand_bills += bills
        lines.append(f"**{label}**: {fmt_money(rev)} — {bills} bill — AOV {fmt_k(aov)}")

    grand_aov = round(grand_rev / grand_bills) if grand_bills else 0
    message = (
        f"**📊 Doanh thu 3 Brand — {header} — {today}**\n\n"
        + "\n".join(lines)
        + f"\n\n**Tổng cộng: {fmt_money(grand_rev)}** ({grand_bills} bill · AOV {fmt_k(grand_aov)})"
    )
    for label, error in failed:
        text = (error or "").lower()
        if "timed out" in text or "timeout" in text:
            reason = "CukCuk phản hồi chậm/timeout — số liệu sẽ tự bù lại ở lần cập nhật kế tiếp"
        elif "login failed" in text or "signature" in text or "401" in text:
            reason = "CukCuk từ chối đăng nhập — cần kiểm tra lại credentials"
        else:
            reason = "CukCuk báo lỗi, cần kiểm tra lại"
        message += f"\n\n⚠️ Không lấy được dữ liệu {label}: {reason}"
    print(message)

    tok = http_json("POST", f"{LARK_BASE}/open-apis/auth/v3/tenant_access_token/internal",
                     payload={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET})
    if tok.get("code") != 0:
        raise RuntimeError(f"Lark auth failed: {tok}")
    access_token = tok["tenant_access_token"]

    resp = http_json("POST", f"{LARK_BASE}/open-apis/im/v1/messages",
                      params={"receive_id_type": "chat_id"},
                      headers={"Authorization": f"Bearer {access_token}"},
                      payload={"receive_id": LARK_CHAT_ID, "msg_type": "text", "content": json.dumps({"text": message})})
    if resp.get("code") != 0:
        raise RuntimeError(f"Lark send failed: {resp}")
    print("Đã gửi báo cáo lên Lark thành công. message_id:", resp["data"]["message_id"])


if __name__ == "__main__":
    main()
