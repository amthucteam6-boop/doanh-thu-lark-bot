"""
Backend cho trang web Doanh thu 3 Brand — phien ban chay tren Vercel (khong
con phu thuoc may Mac/server local). KHONG dung cache file cuc bo (Vercel
serverless khong giu file lau dai giua cac lan goi) - moi thu tinh TRUC TIEP
tu CukCuk moi lan goi. Ghi chu tung brand luu qua GitHub Contents API (repo
nay), vi day la noi duy nhat co the ghi ben vung tu Vercel serverless.

Endpoint duy nhat /api/dashboard, dieu huong theo query param "action":
  GET  ?action=state&date=YYYY-MM-DD        -> doanh thu 3 khung gio + so sanh hom qua
  GET  ?action=month&month=YYYY-MM          -> tong hop theo ngay ca thang
  GET  ?action=online&date=YYYY-MM-DD       -> doanh thu kenh Merchant/Online
  GET  ?action=notes&brand=bpp|waji|39beef  -> lay ghi chu
  POST ?action=notes&brand=...  body {text} -> luu ghi chu (qua GitHub)
"""

import base64
import calendar
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
BRANDS_CUKCUK = {
    "bpp": {"domain": "beardpapa", "app_id": "CUKCUKOpenPlatform", "secret": os.environ.get("CUKCUK_BPP_SECRET", "")},
    "waji": {"domain": "waji", "app_id": "CUKCUKOpenPlatform", "secret": os.environ.get("CUKCUK_WAJI_SECRET", "")},
    "39beef": {"domain": "39beef", "app_id": "CUKCUKOpenPlatform", "secret": os.environ.get("CUKCUK_39BEEF_SECRET", "")},
}

# "Display unit" = don vi hien thi (39Beef tach 2 co so, con lai giu nguyen).
DISPLAY_UNITS = {
    "bpp": {"cukcuk_brand": "bpp", "branch_match": None},
    "waji": {"cukcuk_brand": "waji", "branch_match": None},
    "39beef_th": {"cukcuk_brand": "39beef", "branch_match": "245"},
    "39beef_vph": {"cukcuk_brand": "39beef", "branch_match": "34"},
}
MONTH_UNITS = {
    "bpp": {"cukcuk_brand": "bpp", "branch_match": None},
    "waji": {"cukcuk_brand": "waji", "branch_match": None},
    "39beef": {"cukcuk_brand": "39beef", "branch_match": None},
}

SLOT_CUTOFFS = {"trua": "12:00:00", "chieu": "15:00:00", "toi": "22:00:00"}
SLOT_ORDER = ["trua", "chieu", "toi"]

ONLINE_CHANNEL_PREFIXES = [
    "grabfood", "grab", "shopeefood", "shopee", "foody", "befood",
    "capichi", "dealtoday", "xanh ngon", "kh doanh nghiep", "kh su kien",
]

# 11 co so BPP dang hoat dong that (xac nhan 19/08/2026, gom ca "Hoi Cho" -
# gian hang su kien hoat dong khong lien tuc). Can ra soat lai neu BPP mo/dong
# them co so moi.
BPP_ACTIVE_BRANCHES = [
    "Nguyễn Du", "Lê Đại Hành", "Phan Đình Phùng", "34 Vũ Phạm Hàm",
    "Thái Hà", "Hàng Bông", "Nguyễn Lương Bằng", "Lương Định Của",
    "177 Đội Cấn", "Nguyễn Thị Định", "Hội Chợ",
]

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = "amthucteam6-boop/doanh-thu-lark-bot"
NOTES_PATH_IN_REPO = "data/notes.json"
NOTE_BRANDS = ("bpp", "waji", "39beef")


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


def get_branches(headers, branch_filter=None):
    data = http_json("GET", f"{CUKCUK_BASE}/api/v1/branchs/all", headers=headers)
    branches = [b for b in data.get("Data", []) if not b.get("Inactive") and not b.get("IsBaseDepot")]
    if branch_filter:
        branches = [b for b in branches if any(f in b["Name"] for f in branch_filter)]
    return branches


def fetch_invoices_range(branch_id, since_date_iso, until_date_iso, headers):
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
            if ref_date < since_date_iso or ref_date > until_date_iso:
                continue
            invoices.append(inv)
        if len(data) < 100:
            break
        last_date = (data[-1].get("RefDate") or "")[:10]
        if last_date and last_date < since_date_iso:
            break
        page += 1
    return invoices


def _invoice_amount(inv):
    """So tien thuc cua 1 hoa don. Chi fallback sang Amount khi TotalAmount
    THUC SU THIEU (None) - khong fallback khi TotalAmount = 0 hop le (vd hoa
    don khuyen mai giam 100%). Xem chi tiet trong memory du an."""
    total_amount = inv.get("TotalAmount")
    return float(total_amount) if total_amount is not None else float(inv.get("Amount") or 0)


def classify_online_channel(table_name):
    stripped = re.sub(r"\s*\d+$", "", table_name or "").strip()
    if not stripped:
        return None
    normalized = stripped.lower()
    for prefix in ONLINE_CHANNEL_PREFIXES:
        if normalized.startswith(prefix):
            return stripped
    return None


def fetch_brand_invoices(cukcuk_key, since_date_iso, until_date_iso, branch_filter=None):
    """Dang nhap 1 brand CukCuk that, quet TAT CA chi nhanh song song, tra ve
    list hoa don (da gan them 'BranchName') trong khoang ngay - dung chung cho
    ca state/thang/online, chi 1 lan quet moi khoang ngay du dai bao nhieu."""
    cfg = BRANDS_CUKCUK[cukcuk_key]
    try:
        token = cukcuk_login(cfg)
    except RuntimeError:
        time.sleep(2)
        token = cukcuk_login(cfg)
    headers = cukcuk_headers(token)
    branches = get_branches(headers, branch_filter)

    def _fetch(b):
        invs = fetch_invoices_range(b["Id"], since_date_iso, until_date_iso, headers)
        for inv in invs:
            inv["BranchName"] = b["Name"].strip()
        return invs

    all_invoices = []
    with ThreadPoolExecutor(max_workers=min(len(branches), 10) or 1) as ex:
        for invs in ex.map(_fetch, branches):
            all_invoices.extend(invs)
    return all_invoices


def fetch_real_brands(since_date_iso, until_date_iso):
    """Quet ca 3 brand CukCuk that (bpp/waji/39beef) song song trong 1 khoang
    ngay - dung lam nguon chung, roi map sang display unit / month unit."""
    jobs = {"bpp": BPP_ACTIVE_BRANCHES, "waji": None, "39beef": None}
    raw = {}

    def _job(item):
        key, filt = item
        try:
            return key, fetch_brand_invoices(key, since_date_iso, until_date_iso, filt), None
        except Exception as exc:  # noqa: BLE001
            return key, None, str(exc)

    with ThreadPoolExecutor(max_workers=3) as ex:
        for key, invs, err in ex.map(_job, jobs.items()):
            raw[key] = (invs, err)
    return raw


def fmt_money(v):
    return f"{round(v):,.0f}".replace(",", ".") + " đ"


# --- action=state ---

def _summarize_day(invoices, cutoff=None):
    if cutoff:
        invoices = [i for i in invoices if (i.get("RefDate") or "")[11:19] <= cutoff]
    rev = sum(_invoice_amount(i) for i in invoices)
    bills = len(invoices)
    return {"revenue": rev, "bills": bills, "aov": round(rev / bills) if bills else 0}


def build_state_for_date(raw, date_iso):
    now_iso = datetime.now(ZoneInfo("Asia/Saigon")).isoformat(timespec="seconds")
    brands, errors = {}, {}
    for unit_key, meta in DISPLAY_UNITS.items():
        invs, err = raw[meta["cukcuk_brand"]]
        if err:
            errors[unit_key] = err
            brands[unit_key] = None
            continue
        match = meta["branch_match"]
        day_invs = [i for i in invs if (i.get("RefDate") or "")[:10] == date_iso]
        if match:
            day_invs = [i for i in day_invs if match in (i.get("BranchName") or "")]
        slots = {}
        for slot_key in SLOT_ORDER:
            s = _summarize_day(day_invs, SLOT_CUTOFFS[slot_key])
            s["updated_at"] = now_iso
            slots[slot_key] = s
        brands[unit_key] = slots
    return brands, errors


def _delta(today_val, yday_val):
    if today_val is None or yday_val is None:
        return None
    delta = today_val - yday_val
    pct = (delta / yday_val * 100) if yday_val else None
    return {"today": today_val, "yesterday": yday_val, "delta": delta, "pct": pct}


def action_state(date_iso):
    yesterday = (datetime.strptime(date_iso, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    # Quet 1 lan tu yesterday-00:00 toi date_iso, du cho ca 2 ngay - khong can
    # goi CukCuk rieng cho tung ngay.
    raw = fetch_real_brands(yesterday, date_iso)
    today_brands, errors = build_state_for_date(raw, date_iso)
    yday_brands, _ = build_state_for_date(raw, yesterday)

    compare = {u: {} for u in list(DISPLAY_UNITS) + ["_total"]}
    for slot_key in SLOT_ORDER:
        total_today, total_yday = 0.0, 0.0
        any_today, any_yday = False, False
        for u in DISPLAY_UNITS:
            t = (today_brands.get(u) or {}).get(slot_key)
            y = (yday_brands.get(u) or {}).get(slot_key)
            compare[u][slot_key] = _delta(t["revenue"] if t else None, y["revenue"] if y else None)
            if t:
                total_today += t["revenue"]
                any_today = True
            if y:
                total_yday += y["revenue"]
                any_yday = True
        compare["_total"][slot_key] = _delta(total_today if any_today else None, total_yday if any_yday else None)

    return {"date": date_iso, "yesterday": yesterday, "brands": today_brands, "compare": compare, "errors": errors}


# --- action=month ---

def action_month(year_month):
    year, month = int(year_month[:4]), int(year_month[5:7])
    n_days = calendar.monthrange(year, month)[1]
    now_vn = datetime.now(ZoneInfo("Asia/Saigon"))
    today = now_vn.strftime("%Y-%m-%d")
    month_start = f"{year_month}-01"
    month_end = f"{year_month}-{n_days:02d}"
    fetch_until = min(month_end, today)

    raw = fetch_real_brands(month_start, fetch_until)

    day_totals = {u: {} for u in MONTH_UNITS}
    errors = {}
    for unit_key, meta in MONTH_UNITS.items():
        invs, err = raw[meta["cukcuk_brand"]]
        if err:
            errors[unit_key] = err
            continue
        for inv in invs:
            d = (inv.get("RefDate") or "")[:10]
            slot = day_totals[unit_key].setdefault(d, {"revenue": 0.0, "bills": 0})
            slot["revenue"] += _invoice_amount(inv)
            slot["bills"] += 1

    all_dates = [f"{year_month}-{d:02d}" for d in range(1, n_days + 1)]
    days_out = []
    totals = {u: 0.0 for u in MONTH_UNITS}
    counted = {u: 0 for u in MONTH_UNITS}
    for d in all_dates:
        row = {"date": d}
        for u in MONTH_UNITS:
            if d > today:
                row[u] = None
                continue
            rec = day_totals[u].get(d)
            row[u] = rec["revenue"] if rec else 0.0
            totals[u] += row[u]
            counted[u] += 1
        days_out.append(row)

    return {"month": year_month, "days": days_out, "totals": totals, "counted": counted, "errors": errors}


# --- action=online ---

def action_online(date_iso):
    raw = fetch_real_brands(date_iso, date_iso)
    result = {}
    errors = {}
    for cukcuk_key, (invs, err) in raw.items():
        if err:
            errors[cukcuk_key] = err
            continue
        by_branch = {}
        for inv in invs:
            name = inv.get("BranchName") or "?"
            entry = by_branch.setdefault(name, {"revenue": 0.0, "bills": 0, "total_revenue": 0.0, "total_bills": 0, "by_channel": {}})
            amt = _invoice_amount(inv)
            entry["total_revenue"] += amt
            entry["total_bills"] += 1
            ch = classify_online_channel(inv.get("TableName") or "")
            if ch:
                entry["revenue"] += amt
                entry["bills"] += 1
                slot = entry["by_channel"].setdefault(ch, {"revenue": 0.0, "bills": 0})
                slot["revenue"] += amt
                slot["bills"] += 1
        total_revenue = sum(v["revenue"] for v in by_branch.values())
        total_bills = sum(v["bills"] for v in by_branch.values())
        result[cukcuk_key] = {"by_branch": by_branch, "total_revenue": total_revenue, "total_bills": total_bills}
    return {"date": date_iso, "brands": result, "errors": errors}


# --- action=notes (GitHub Contents API lam kho luu ben vung) ---

def github_api(method, path, payload=None):
    if not GITHUB_TOKEN:
        raise RuntimeError("Chưa cấu hình GITHUB_TOKEN trên Vercel — ghi chú chưa lưu được.")
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        method=method,
    )
    req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"GitHub API lỗi {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from None


def _load_notes_file():
    current = github_api("GET", f"/repos/{GITHUB_REPO}/contents/{NOTES_PATH_IN_REPO}")
    if current is None:
        return {}, None
    content = base64.b64decode(current["content"]).decode("utf-8")
    notes = json.loads(content) if content.strip() else {}
    return notes, current["sha"]


def action_notes_get(brand):
    notes, _ = _load_notes_file()
    return notes.get(brand, {"text": "", "updated_at": None})


def action_notes_save(brand, text):
    notes, sha = _load_notes_file()
    entry = {"text": text, "updated_at": datetime.now(ZoneInfo("Asia/Saigon")).isoformat(timespec="seconds")}
    notes[brand] = entry
    content_b64 = base64.b64encode(json.dumps(notes, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii")
    payload = {"message": f"Cap nhat ghi chu {brand} tu dashboard web", "content": content_b64}
    if sha:
        payload["sha"] = sha
    github_api("PUT", f"/repos/{GITHUB_REPO}/contents/{NOTES_PATH_IN_REPO}", payload)
    return entry


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        action = (params.get("action") or [""])[0]
        try:
            if action == "state":
                date_iso = (params.get("date") or [datetime.now(ZoneInfo("Asia/Saigon")).strftime("%Y-%m-%d")])[0]
                self._respond(200, action_state(date_iso))
            elif action == "month":
                year_month = (params.get("month") or [datetime.now(ZoneInfo("Asia/Saigon")).strftime("%Y-%m")])[0]
                self._respond(200, action_month(year_month))
            elif action == "online":
                date_iso = (params.get("date") or [datetime.now(ZoneInfo("Asia/Saigon")).strftime("%Y-%m-%d")])[0]
                self._respond(200, action_online(date_iso))
            elif action == "notes":
                brand = (params.get("brand") or [""])[0]
                if brand not in NOTE_BRANDS:
                    self._respond(400, {"error": f"brand không hợp lệ: {brand}"})
                    return
                self._respond(200, action_notes_get(brand))
            else:
                self._respond(400, {"error": f"action không hợp lệ: {action!r}"})
        except Exception as exc:  # noqa: BLE001
            self._respond(500, {"error": str(exc)})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        action = (params.get("action") or [""])[0]
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid json"})
            return
        try:
            if action == "notes":
                brand = (params.get("brand") or [""])[0]
                if brand not in NOTE_BRANDS:
                    self._respond(400, {"error": f"brand không hợp lệ: {brand}"})
                    return
                entry = action_notes_save(brand, body.get("text", ""))
                self._respond(200, entry)
            else:
                self._respond(400, {"error": f"action không hợp lệ: {action!r}"})
        except Exception as exc:  # noqa: BLE001
            self._respond(500, {"error": str(exc)})

    def _respond(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
