"""
Gui bao cao doanh thu 3 brand (kem doanh thu kenh Merchant/Online) vao nhom
Lark - slot "trua" CO DINH. File RIENG cho tung khung gio (khong dung chung
1 file + query param nua) vi Vercel Cron Jobs coi 2 URL cung path khac query
string la 1 route - da xac nhan thuc te chi 1/3 cron duoc dang ky khi dung
chung path. Trigger boi Vercel Cron Jobs (xem "crons" trong vercel.json) -
chay tren cloud 24/7, KHONG phu thuoc may Mac bat/tat.

Bao ve bang CRON_SECRET (Vercel tu dong gui header Authorization: Bearer
<CRON_SECRET> cho request tu chinh Cron Job cua no).

QUAN TRONG: tinh doanh thu luy ke toi GIO THUC TE luc gui (khong phai moc co
dinh 12:00/15:00/22:00 nua) - doi lai ngay 2026-08-18 vi don hang phat sinh
vai phut ngay sau moc co dinh (vd 22:04) bi loai khoi bao cao "Toi" trong khi
thuc te da xay ra truoc luc tin nhan gui, gay hieu lam la thieu du lieu. Nhan
"Trua/Chieu/Toi" gio chi con la TEN goi cua tung lan chay trong ngay, khong
con la cam ket "dung so tai chinh xac mot moc". Van giu safeguard: neu bi
delay qua nua dem thi tinh cho ngay hom truoc.
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
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

CUKCUK_BASE = "https://graphapi.cukcuk.vn"
LARK_BASE = "https://open.larksuite.com"
LARK_CHAT_ID = "oc_dfe2d8bb5344bb4f27a05e37d8b56408"  # nhom "BÁO CÁO MARKETING DAILY"

BRANDS_CUKCUK = {
    "bpp": {"domain": "beardpapa", "app_id": "CUKCUKOpenPlatform", "secret": os.environ.get("CUKCUK_BPP_SECRET", "")},
    "waji": {"domain": "waji", "app_id": "CUKCUKOpenPlatform", "secret": os.environ.get("CUKCUK_WAJI_SECRET", "")},
    "39beef": {"domain": "39beef", "app_id": "CUKCUKOpenPlatform", "secret": os.environ.get("CUKCUK_39BEEF_SECRET", "")},
}

LARK_APP_ID = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
CRON_SECRET = os.environ.get("CRON_SECRET", "")

SLOT_CUTOFFS = {"trua": "12:00:00", "chieu": "15:00:00", "toi": "22:00:00"}
SLOT_LABELS = {"trua": "Trưa (12h)", "chieu": "Chiều (15h)", "toi": "Tối (22h)"}

ONLINE_CHANNEL_PREFIXES = [
    "grabfood", "grab", "shopeefood", "shopee", "foody", "befood",
    "capichi", "dealtoday", "xanh ngon", "kh doanh nghiep", "kh su kien",
]

# 10 co so BPP dang hoat dong that (xac nhan 17/08/2026). Can ra soat lai neu
# BPP mo/dong them co so moi - xem ghi chu day du trong cukcuk_client.py.
BPP_ACTIVE_BRANCHES = [
    "Nguyễn Du", "Lê Đại Hành", "Phan Đình Phùng", "34 Vũ Phạm Hàm",
    "Thái Hà", "Hàng Bông", "Nguyễn Lương Bằng", "Lương Định Của",
    "177 Đội Cấn", "Nguyễn Thị Định", "Hội Chợ",
]


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


def classify_online_channel(table_name):
    stripped = re.sub(r"\s*\d+$", "", table_name or "").strip()
    if not stripped:
        return None
    normalized = stripped.lower()
    for prefix in ONLINE_CHANNEL_PREFIXES:
        if normalized.startswith(prefix):
            return stripped
    return None


def _invoice_amount(inv):
    """So tien thuc cua 1 hoa don. Chi lay Amount lam fallback khi TotalAmount
    THUC SU THIEU (None/khong co key) - KHONG fallback khi TotalAmount = 0 hop
    le (vd hoa don khuyen mai giam 100%, khach khong tra tien). Dung 'or' don
    gian se coi 0 la falsy roi nham lay gia truoc khuyen mai, gay dem thua
    doanh thu (da xay ra that: 4 hoa don Waji giam 100% ~257k/hoa don, sai
    lech dung 1.028.000d khi hoi khoang ngay 1/8-13/8)."""
    total_amount = inv.get("TotalAmount")
    return float(total_amount) if total_amount is not None else float(inv.get("Amount") or 0)


def fetch_brand_at_cutoff(cukcuk_key, date_iso, cutoff_time, branch_filter=None):
    """Doanh thu (tong + kenh online) luy ke tu dau ngay toi cutoff_time
    (HH:MM:SS) cua 1 brand CukCuk - dung cho bao cao lich co dinh."""
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

    def _fetch(b):
        name = b["Name"].strip()
        rev, bills = 0.0, 0
        online_channel = {}
        online_rev, online_bills = 0.0, 0
        for inv in fetch_invoices_since(b["Id"], date_iso, headers):
            ref_time = (inv.get("RefDate") or "")[11:19]
            if ref_time > cutoff_time:
                continue
            amt = float(_invoice_amount(inv))
            rev += amt
            bills += 1
            ch = classify_online_channel(inv.get("TableName") or "")
            if ch:
                online_rev += amt
                online_bills += 1
                slot = online_channel.setdefault(ch, {"revenue": 0.0, "bills": 0})
                slot["revenue"] += amt
                slot["bills"] += 1
        return name, {"revenue": rev, "bills": bills}, {"revenue": online_rev, "bills": online_bills, "by_channel": online_channel}

    by_branch, by_branch_online = {}, {}
    with ThreadPoolExecutor(max_workers=min(len(branches), 10) or 1) as ex:
        for name, total, online in ex.map(_fetch, branches):
            by_branch[name] = total
            by_branch_online[name] = online
    return by_branch, by_branch_online


def _merge_channels(by_branch_online):
    merged = {}
    for data in by_branch_online.values():
        for ch, v in data["by_channel"].items():
            slot = merged.setdefault(ch, {"revenue": 0.0, "bills": 0})
            slot["revenue"] += v["revenue"]
            slot["bills"] += v["bills"]
    return merged


def _channel_summary(by_channel):
    if not by_channel:
        return None
    parts = [f"{name} {fmt_k(v['revenue'])}" for name, v in sorted(by_channel.items(), key=lambda x: -x[1]["revenue"])]
    return ", ".join(parts)


def fmt_money(v):
    return f"{round(v):,.0f}".replace(",", ".") + " đ"


def fmt_k(v):
    return f"{round(v / 1000):,.0f}".replace(",", ".") + "k"


def build_scheduled_message(slot, date_iso, cutoff):
    # cutoff = gio THUC TE luc gui (khong dung moc co dinh SLOT_CUTOFFS nua) -
    # tranh don hang phat sinh vai phut sau moc danh nghia (vd 22:04) bi loai
    # khoi bao cao "Toi", trong khi thuc te da xay ra truoc luc tin nhan gui.
    label = SLOT_LABELS[slot]

    jobs = [("bpp", BPP_ACTIVE_BRANCHES), ("waji", None), ("39beef", None)]

    def _job(item):
        key, branch_filter = item
        try:
            return key, fetch_brand_at_cutoff(key, date_iso, cutoff, branch_filter), None
        except Exception as exc:  # noqa: BLE001
            return key, None, exc

    raw = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        for key, result, exc in ex.map(_job, jobs):
            raw[key] = (result, exc)

    UNIT_ICON = {"bpp": "🥐", "waji": "🍚", "39beef": "🥩"}

    lines = []
    grand_rev, grand_bills, grand_online = 0.0, 0, 0.0

    def emit(unit_label, cukcuk_key, match_substr=None):
        nonlocal grand_rev, grand_bills, grand_online
        result, exc = raw[cukcuk_key]
        if exc is not None:
            lines.append(f"⚠️ **{unit_label}**: lỗi lấy dữ liệu ({str(exc)[:80]})")
            lines.append("")
            return
        by_branch, by_branch_online = result
        if match_substr:
            found = next((v for k, v in by_branch.items() if match_substr in k), {"revenue": 0.0, "bills": 0})
            found_online = next((v for k, v in by_branch_online.items() if match_substr in k), None)
            rev, bills = found["revenue"], found["bills"]
            online_rev = found_online["revenue"] if found_online else 0.0
            online_by_channel = found_online["by_channel"] if found_online else {}
        else:
            rev = sum(v["revenue"] for v in by_branch.values())
            bills = sum(v["bills"] for v in by_branch.values())
            online_rev = sum(v["revenue"] for v in by_branch_online.values())
            online_by_channel = _merge_channels(by_branch_online)

        aov = round(rev / bills) if bills else 0
        icon = UNIT_ICON.get(cukcuk_key, "🔸")
        lines.append(f"{icon} **{unit_label}**: {fmt_money(rev)} · {bills} bill · AOV {fmt_k(aov)}")
        # Luon hien dong Online (ke ca 0d) - an di khi = 0 khien Anh tuong nham la
        # bo sot/quen tinh, trong khi thuc ra chi la chua co don online luc do.
        summary = _channel_summary(online_by_channel)
        online_line = f"　↳ Online {fmt_money(online_rev)}"
        if summary:
            online_line += f" ({summary})"
        lines.append(online_line)
        lines.append("")
        grand_rev += rev
        grand_bills += bills
        grand_online += online_rev

    emit("Beard Papa's", "bpp")
    emit("Waji", "waji")
    emit("39Beef - 245 Tô Hiệu", "39beef", "245")
    emit("39Beef - 34 Vũ Phạm Hàm", "39beef", "34")

    grand_aov = round(grand_rev / grand_bills) if grand_bills else 0
    body = "\n".join(lines).rstrip()
    message = f"**📊 Doanh thu 3 Brand — {label} — {date_iso}**\n\n{body}"
    message += f"\n\n━━━━━━━━━━━━━━━\n**Tổng cộng: {fmt_money(grand_rev)}** · {grand_bills} bill · AOV {fmt_k(grand_aov)}"
    message += f"\n**Merchant/Online: {fmt_money(grand_online)}**"
    return message


def lark_token():
    tok = http_json("POST", f"{LARK_BASE}/open-apis/auth/v3/tenant_access_token/internal",
                     payload={"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET})
    if tok.get("code") != 0:
        raise RuntimeError(f"Lark auth failed: {tok}")
    return tok["tenant_access_token"]


def send_to_lark(text):
    access_token = lark_token()
    resp = http_json(
        "POST", f"{LARK_BASE}/open-apis/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        headers={"Authorization": f"Bearer {access_token}"},
        payload={"receive_id": LARK_CHAT_ID, "msg_type": "text", "content": json.dumps({"text": text})},
    )
    if resp.get("code") != 0:
        raise RuntimeError(f"Lark send failed: {resp}")


SLOT = "trua"  # hardcode co dinh - moi file 1 slot, khong doc tu query nua


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if CRON_SECRET:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {CRON_SECRET}":
                self._respond(401, {"ok": False, "error": "unauthorized"})
                return

        slot = SLOT
        now_vn = datetime.now(ZoneInfo("Asia/Saigon"))
        # De phong Vercel Cron bi delay qua nua dem - van tinh cho ngay hom truoc.
        date_iso = (now_vn - timedelta(days=1)).strftime("%Y-%m-%d") if now_vn.hour < 6 else now_vn.strftime("%Y-%m-%d")

        try:
            message = build_scheduled_message(slot, date_iso, now_vn.strftime("%H:%M:%S"))
            send_to_lark(message)
            self._respond(200, {"ok": True, "slot": slot, "date": date_iso})
        except Exception as exc:  # noqa: BLE001
            self._respond(500, {"ok": False, "error": str(exc)})

    def _respond(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))
