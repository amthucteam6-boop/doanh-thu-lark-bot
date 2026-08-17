"""
Bot 2 chieu: nhan tin nhan hoi doanh thu trong nhom Lark, tra loi ngay bang so
lieu lay truc tiep tu CukCuk (khong phu thuoc data luu tren may Mac, nen chay
duoc ca khi may tat). Deploy tren Vercel (chay 24/7, nhan webhook tu Lark Event
Subscription) - KHAC voi report.py (chi gui 1 chieu theo lich/lenh).

Nhan dien vai cau co dinh: "doanh thu hom nay", "doanh thu thang nay", co the
kem ten brand (bpp/waji/39beef) de loc rieng. Mac dinh (khong noi brand/thang)
= doanh thu hom nay ca 4 don vi.
"""

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

CUKCUK_BASE = "https://graphapi.cukcuk.vn"
LARK_BASE = "https://open.larksuite.com"

BRANDS_CUKCUK = {
    "bpp": {"domain": "beardpapa", "app_id": "CUKCUKOpenPlatform", "secret": os.environ.get("CUKCUK_BPP_SECRET", "")},
    "waji": {"domain": "waji", "app_id": "CUKCUKOpenPlatform", "secret": os.environ.get("CUKCUK_WAJI_SECRET", "")},
    "39beef": {"domain": "39beef", "app_id": "CUKCUKOpenPlatform", "secret": os.environ.get("CUKCUK_39BEEF_SECRET", "")},
}
DISPLAY_UNITS_TODAY = [
    ("bpp", "Beard Papa's", "bpp", None),
    ("waji", "Waji", "waji", None),
    ("39beef_th", "39Beef - 245 Tô Hiệu", "39beef", "245"),
    ("39beef_vph", "39Beef - 34 Vũ Phạm Hàm", "39beef", "34"),
]
DISPLAY_UNITS_MONTH = [
    ("bpp", "Beard Papa's", "bpp", None),
    ("waji", "Waji", "waji", None),
    ("39beef", "39Beef", "39beef", None),
]

LARK_APP_ID = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
LARK_VERIFY_TOKEN = os.environ.get("LARK_VERIFY_TOKEN", "")


def http_json(method, url, payload=None, headers=None, params=None, timeout=25, retries=3):
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
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Timeout/network error for {url}: {last_exc}")


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


def fetch_invoices_since(branch_id, since_date_iso, headers):
    """Lay hoa don tu since_date_iso (00:00) toi hien tai trong 1 lan quet - dung
    chung cho ca 'hom nay' (since = hom nay) lan 'thang nay' (since = ngay 1 dau
    thang), khong can goi rieng tung ngay."""
    since = since_date_iso + "T00:00:00"
    page, invoices = 1, []
    while True:
        r = http_json("POST", f"{CUKCUK_BASE}/api/v1/sainvoices/paging", headers=headers,
                       payload={"Page": page, "Limit": 100, "BranchId": branch_id, "LastSyncDate": since})
        data = r.get("Data", [])
        for inv in data:
            if inv.get("PaymentStatus") in (4, 5):
                continue
            ref_date = (inv.get("RefDate") or "")[:10]
            if ref_date >= since_date_iso:
                invoices.append(inv)
        if len(data) < 100:
            break
        last_date = (data[-1].get("RefDate") or "")[:10]
        if last_date and last_date < since_date_iso:
            break
        page += 1
    return invoices


def fetch_brand_range(cukcuk_key, since_date_iso):
    cfg = BRANDS_CUKCUK[cukcuk_key]
    # CukCuk thinh thoang tu choi tam thoi khi nhieu brand dang nhap gan nhau
    # (loi ung dung Code:200 kem ErrorType, khong phai loi mang nen retry o
    # http_json khong bat duoc) - thu lai 1 lan sau 2s truoc khi bao loi han.
    try:
        token = cukcuk_login(cfg)
    except RuntimeError:
        time.sleep(2)
        token = cukcuk_login(cfg)
    headers = cukcuk_headers(token)
    branches = get_branches(headers)
    by_id = {b["Id"]: b["Name"] for b in branches}
    all_inv = []
    for b in branches:
        for inv in fetch_invoices_since(b["Id"], since_date_iso, headers):
            inv["BranchId"] = b["Id"]
            all_inv.append(inv)
    per_branch = {}
    for inv in all_inv:
        bid = inv.get("BranchId")
        rev = float(inv.get("TotalAmount") or inv.get("Amount") or 0)
        d = per_branch.setdefault(bid, {"revenue": 0.0, "bills": 0})
        d["revenue"] += rev
        d["bills"] += 1
    return {by_id.get(bid, bid): agg for bid, agg in per_branch.items()}


def fmt_money(v):
    return f"{round(v):,.0f}".replace(",", ".") + " đ"


def fmt_k(v):
    return f"{round(v / 1000):,.0f}".replace(",", ".") + "k"


BRAND_KEYWORDS = {
    "bpp": ["bpp", "beard papa", "beardpapa", "su kem"],
    "waji": ["waji"],
    "39beef": ["39beef", "39 beef", "gyukatsu"],
}


def detect_brand(text):
    t = text.lower()
    for key, kws in BRAND_KEYWORDS.items():
        if any(kw in t for kw in kws):
            return key
    return None


def detect_period(text):
    t = text.lower()
    if "tháng" in t or "thang" in t:
        return "month"
    return "today"


def detect_report_type(text):
    t = text.lower()
    if "merchant" in t or "online" in t:
        return "online"
    return "revenue"


# --- Doanh thu kenh Merchant/Online (Grab/Foody/Shopee/Befood/...) ---
#
# CukCuk gan ten kenh online vao TableName cua hoa don (vd "Grab1", "Foody1").
# O BPP ban vat ly luon de trong nen "TableName khong rong" = online. Nhung o
# Waji/39Beef (phuc vu tai ban that), TableName thuong la SO BAN THAT - phai
# nhan dien theo dung ten kenh da biet, khong the dung "khong rong = online".
ONLINE_CHANNEL_PREFIXES = [
    "grabfood", "grab", "shopeefood", "shopee", "foody", "befood",
    "capichi", "dealtoday", "xanh ngon", "kh doanh nghiep", "kh su kien",
]

# 10 co so BPP dang hoat dong that (xac nhan 17/08/2026 - 6 chi nhanh CukCuk
# tra ve nhung 0 hoat dong nhieu ngay lien, gom 2 cai da ghi "(closed)"). Can
# ra soat lai neu BPP mo/dong them co so moi.
BPP_ACTIVE_BRANCHES = [
    "Nguyễn Du", "Lê Đại Hành", "Phan Đình Phùng", "34 Vũ Phạm Hàm",
    "Thái Hà", "Hàng Bông", "Nguyễn Lương Bằng", "Lương Định Của",
    "177 Đội Cấn", "Nguyễn Thị Định",
]


def classify_online_channel(table_name):
    stripped = re.sub(r"\s*\d+$", "", table_name or "").strip()
    if not stripped:
        return None
    normalized = stripped.lower()
    for prefix in ONLINE_CHANNEL_PREFIXES:
        if normalized.startswith(prefix):
            return stripped
    return None


def fetch_online_revenue(cukcuk_key, since_date_iso, branch_filter=None):
    cfg = BRANDS_CUKCUK[cukcuk_key]
    try:
        token = cukcuk_login(cfg)
    except RuntimeError:
        time.sleep(2)
        token = cukcuk_login(cfg)
    headers = cukcuk_headers(token)
    branches = get_branches(headers)
    if branch_filter:
        branches = [b for b in branches if any(f in b["Name"] for f in branch_filter)]

    def _fetch_branch(b):
        name = b["Name"].strip()
        rev, bills = 0.0, 0
        for inv in fetch_invoices_since(b["Id"], since_date_iso, headers):
            if classify_online_channel(inv.get("TableName") or "") is None:
                continue
            rev += float(inv.get("TotalAmount") or 0)
            bills += 1
        return name, {"revenue": rev, "bills": bills}

    by_branch = {}
    with ThreadPoolExecutor(max_workers=min(len(branches), 10) or 1) as ex:
        for name, data in ex.map(_fetch_branch, branches):
            by_branch[name] = data
    return by_branch


def build_online_answer(text):
    period = detect_period(text)
    brand_filter = detect_brand(text)
    now_vn = datetime.now(ZoneInfo("Asia/Saigon"))

    if period == "month":
        since_date = now_vn.strftime("%Y-%m-01")
        header = f"Doanh thu kênh Merchant/Online — tháng {now_vn.month}/{now_vn.year} (tính đến {now_vn.strftime('%d/%m %H:%M')})"
    else:
        since_date = now_vn.strftime("%Y-%m-%d")
        header = f"Doanh thu kênh Merchant/Online — hôm nay {now_vn.strftime('%d/%m/%Y')} (tính đến {now_vn.strftime('%H:%M')})"

    lines = []
    grand_total, grand_bills = 0.0, 0

    def add_brand(cukcuk_key, label, branch_filter_names, split_branches):
        nonlocal grand_total, grand_bills
        try:
            by_branch = fetch_online_revenue(cukcuk_key, since_date, branch_filter_names)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"⚠️ {label}: lỗi lấy dữ liệu ({str(exc)[:80]})")
            return
        if split_branches:
            for name, data in sorted(by_branch.items(), key=lambda x: -x[1]["revenue"]):
                lines.append(f"**{name}**: {fmt_money(data['revenue'])} — {data['bills']} bill")
                grand_total += data["revenue"]
                grand_bills += data["bills"]
        else:
            total_rev = sum(v["revenue"] for v in by_branch.values())
            total_bills = sum(v["bills"] for v in by_branch.values())
            lines.append(f"**{label}**: {fmt_money(total_rev)} — {total_bills} bill")
            grand_total += total_rev
            grand_bills += total_bills

    if brand_filter in (None, "bpp"):
        add_brand("bpp", "Beard Papa's (BPP) - 10 cơ sở", BPP_ACTIVE_BRANCHES, split_branches=False)
    if brand_filter in (None, "waji"):
        add_brand("waji", "Waji", None, split_branches=False)
    if brand_filter in (None, "39beef"):
        add_brand("39beef", "39Beef", None, split_branches=True)

    message = f"**📊 {header}**\n\n" + "\n".join(lines)
    if brand_filter is None:
        message += f"\n\n**Tổng cộng: {fmt_money(grand_total)}** ({grand_bills} bill)"
    return message


def build_answer(text):
    period = detect_period(text)
    brand_filter = detect_brand(text)
    now_vn = datetime.now(ZoneInfo("Asia/Saigon"))

    if period == "month":
        since_date = now_vn.strftime("%Y-%m-01")
        header = f"Doanh thu tháng {now_vn.month}/{now_vn.year} (tính đến {now_vn.strftime('%d/%m %H:%M')})"
        all_units = DISPLAY_UNITS_MONTH
    else:
        since_date = now_vn.strftime("%Y-%m-%d")
        header = f"Doanh thu hôm nay {now_vn.strftime('%d/%m/%Y')} (tính đến {now_vn.strftime('%H:%M')})"
        all_units = DISPLAY_UNITS_TODAY

    units = [u for u in all_units if u[2] == brand_filter] if brand_filter else all_units

    cache, errors = {}, {}
    lines = []
    grand_rev, grand_bills = 0.0, 0
    for unit_key, label, cukcuk_key, match in units:
        if cukcuk_key not in cache and cukcuk_key not in errors:
            try:
                cache[cukcuk_key] = fetch_brand_range(cukcuk_key, since_date)
            except Exception as exc:  # noqa: BLE001
                errors[cukcuk_key] = str(exc)
        if cukcuk_key in errors:
            lines.append(f"⚠️ {label}: lỗi lấy dữ liệu ({errors[cukcuk_key][:80]})")
            continue
        by_branch = cache[cukcuk_key]
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

    message = f"**📊 {header}**\n\n" + "\n".join(lines)
    if len(units) > 1:
        grand_aov = round(grand_rev / grand_bills) if grand_bills else 0
        message += f"\n\n**Tổng cộng: {fmt_money(grand_rev)}** ({grand_bills} bill · AOV {fmt_k(grand_aov)})"
    return message


def lark_token():
    tok = http_json("POST", f"{LARK_BASE}/open-apis/auth/v3/tenant_access_token/internal",
                     payload={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET})
    if tok.get("code") != 0:
        raise RuntimeError(f"Lark auth failed: {tok}")
    return tok["tenant_access_token"]


def reply_lark(chat_id, text, reply_to_message_id=None):
    access_token = lark_token()
    if reply_to_message_id:
        url = f"{LARK_BASE}/open-apis/im/v1/messages/{reply_to_message_id}/reply"
        payload = {"msg_type": "text", "content": json.dumps({"text": text})}
        params = None
    else:
        url = f"{LARK_BASE}/open-apis/im/v1/messages"
        payload = {"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": text})}
        params = {"receive_id_type": "chat_id"}
    resp = http_json("POST", url, params=params, headers={"Authorization": f"Bearer {access_token}"}, payload=payload)
    if resp.get("code") != 0:
        raise RuntimeError(f"Lark reply failed: {resp}")


TRIGGER_KEYWORDS = ["doanh thu", "doanh số"]

# Tranh tra loi lap khi Lark retry cung 1 event (do timeout/mang) - nho tam cac
# event_id da xu ly trong bo nho tien trinh (du dung vi retry thuong den rat
# gan nhau, cung 1 lan cold-start; qua lan cold-start moi thi Lark cung da bo
# retry roi nen khong sao).
_seen_event_ids = set()


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid json"})
            return

        # Lark URL verification handshake - chi xay ra 1 lan luc cau hinh Event
        # Subscription tren Lark Developer Console.
        if body.get("type") == "url_verification":
            if LARK_VERIFY_TOKEN and body.get("token") != LARK_VERIFY_TOKEN:
                self._respond(403, {"error": "invalid token"})
                return
            self._respond(200, {"challenge": body.get("challenge")})
            return

        header = body.get("header", {})
        if LARK_VERIFY_TOKEN and header.get("token") != LARK_VERIFY_TOKEN:
            self._respond(403, {"error": "invalid token"})
            return

        # Ack ngay de Lark khong coi la loi/retry - xu ly va goi reply API rieng sau.
        self._respond(200, {"code": 0})

        if header.get("event_type") != "im.message.receive_v1":
            return

        event_id = header.get("event_id")
        if event_id in _seen_event_ids:
            return
        _seen_event_ids.add(event_id)

        event = body.get("event", {})
        message = event.get("message", {})
        if message.get("message_type") != "text":
            return

        try:
            text = json.loads(message.get("content", "{}")).get("text", "")
        except json.JSONDecodeError:
            text = ""

        if not any(kw in text.lower() for kw in TRIGGER_KEYWORDS):
            return

        chat_id = message.get("chat_id")
        message_id = message.get("message_id")
        try:
            answer = build_online_answer(text) if detect_report_type(text) == "online" else build_answer(text)
        except Exception as exc:  # noqa: BLE001
            answer = f"⚠️ Có lỗi khi lấy doanh thu: {exc}"

        try:
            reply_lark(chat_id, answer, reply_to_message_id=message_id)
        except Exception:
            pass  # da respond 200 cho Lark roi - loi o day chi xem duoc qua Vercel dashboard logs

    def _respond(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))
