from flask import Flask, request, jsonify, redirect, url_for, session, render_template_string
import os
import sqlite3
import datetime
import secrets
import string
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key-in-render")

DB_PATH = os.environ.get("LICENSE_DB_PATH", "smartpay_licenses.db")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "CHANGE_ME_ADMIN_KEY")
PRODUCT_CODE = "SMARTPAY_INDIA"

PLAN_DAYS = {
    "TRIAL_30D": 30,
    "1YEAR": 365,
    "2YEAR": 365 * 2,
    "3YEAR": 365 * 3,
    "LIFETIME": None,
}

def today_str():
    return datetime.date.today().isoformat()

def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT UNIQUE NOT NULL,
                product_code TEXT NOT NULL DEFAULT 'SMARTPAY_INDIA',
                plan TEXT NOT NULL,
                customer_name TEXT,
                customer_email TEXT,
                customer_phone TEXT,
                device_id TEXT,
                status TEXT NOT NULL DEFAULT 'unused',
                issue_date TEXT NOT NULL,
                activated_at TEXT,
                expiry_date TEXT,
                last_verified_at TEXT,
                max_branches INTEGER DEFAULT 999,
                max_employees INTEGER DEFAULT 999,
                notes TEXT
            )
        """)
        con.commit()

init_db()

def make_license_key(plan):
    prefix_map = {
        "TRIAL_30D": "SMARTPAY-TRIAL-30D",
        "1YEAR": "SMARTPAY-1Y",
        "2YEAR": "SMARTPAY-2Y",
        "3YEAR": "SMARTPAY-3Y",
        "LIFETIME": "SMARTPAY-LIFE",
    }
    prefix = prefix_map.get(plan, "SMARTPAY")
    alphabet = string.ascii_uppercase + string.digits
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return prefix + "-" + "-".join(parts)

def calculate_expiry(plan):
    days = PLAN_DAYS.get(plan)
    if days is None:
        return None
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()

def row_to_dict(row):
    return {k: row[k] for k in row.keys()}

def validate_payload(data):
    license_key = (data.get("license_key") or data.get("key") or "").strip().upper()
    device_id = (data.get("device_id") or data.get("machine_id") or data.get("customer_id") or "").strip()
    firm_name = (data.get("firm_name") or data.get("customer_name") or "").strip()
    product_code = (data.get("product_code") or PRODUCT_CODE).strip().upper()
    return license_key, device_id, firm_name, product_code

def check_admin_api():
    supplied = request.headers.get("X-Admin-Key") or request.args.get("admin_key") or (request.json or {}).get("admin_key") if request.is_json else None
    return supplied == ADMIN_KEY

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("admin_logged_in"):
            return fn(*args, **kwargs)
        return redirect(url_for("admin_login"))
    return wrapper

@app.route("/")
def home():
    return "SmartPay India License API Running"

@app.route("/health")
@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "product": "SmartPay India",
        "product_code": PRODUCT_CODE,
        "time": now_str()
    })

@app.route("/api/license/activate", methods=["POST"])
@app.route("/activate", methods=["POST"])
def activate():
    data = request.get_json(silent=True) or {}
    license_key, device_id, firm_name, product_code = validate_payload(data)

    if not license_key or not device_id:
        return jsonify({"status": "error", "message": "License key and Customer ID / Device ID are required"}), 400

    with db() as con:
        row = con.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,)).fetchone()
        if not row:
            return jsonify({"status": "error", "message": "Invalid license key"}), 400

        lic = row_to_dict(row)

        if lic["product_code"] != product_code:
            return jsonify({"status": "error", "message": "License is not valid for this product"}), 403

        if lic["status"] == "blocked":
            return jsonify({"status": "error", "message": "License is blocked. Please contact support."}), 403

        if lic["expiry_date"]:
            expiry = datetime.datetime.strptime(lic["expiry_date"], "%Y-%m-%d").date()
            if datetime.date.today() > expiry:
                con.execute("UPDATE licenses SET status=? WHERE license_key=?", ("expired", license_key))
                con.commit()
                return jsonify({"status": "expired", "message": "License expired"}), 403

        if lic["device_id"] and lic["device_id"] != device_id:
            return jsonify({"status": "error", "message": "License already used on another device"}), 403

        if not lic["device_id"]:
            con.execute("""
                UPDATE licenses
                SET device_id=?, status='active', activated_at=?, customer_name=COALESCE(NULLIF(?, ''), customer_name), last_verified_at=?
                WHERE license_key=?
            """, (device_id, now_str(), firm_name, now_str(), license_key))
        else:
            con.execute("UPDATE licenses SET status='active', last_verified_at=? WHERE license_key=?", (now_str(), license_key))

        con.commit()
        row = con.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,)).fetchone()
        lic = row_to_dict(row)

    return jsonify({
        "status": "success",
        "message": "License activated successfully",
        "license_key": license_key,
        "product_code": lic["product_code"],
        "plan": lic["plan"],
        "expiry_date": lic["expiry_date"],
        "customer_name": lic["customer_name"],
        "max_branches": lic["max_branches"],
        "max_employees": lic["max_employees"],
        "server_time": now_str()
    })

@app.route("/api/license/verify", methods=["POST"])
@app.route("/validate", methods=["POST"])
def verify():
    data = request.get_json(silent=True) or {}
    license_key, device_id, firm_name, product_code = validate_payload(data)

    if not license_key or not device_id:
        return jsonify({"status": "error", "message": "License key and Customer ID / Device ID are required"}), 400

    with db() as con:
        row = con.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,)).fetchone()
        if not row:
            return jsonify({"status": "error", "message": "Invalid license key"}), 400

        lic = row_to_dict(row)

        if lic["product_code"] != product_code:
            return jsonify({"status": "error", "message": "License is not valid for this product"}), 403

        if lic["status"] == "blocked":
            return jsonify({"status": "error", "message": "License is blocked"}), 403

        if lic["device_id"] != device_id:
            return jsonify({"status": "error", "message": "Unauthorized device"}), 403

        if lic["expiry_date"]:
            expiry = datetime.datetime.strptime(lic["expiry_date"], "%Y-%m-%d").date()
            if datetime.date.today() > expiry:
                con.execute("UPDATE licenses SET status=? WHERE license_key=?", ("expired", license_key))
                con.commit()
                return jsonify({"status": "expired", "message": "License expired"}), 403

        con.execute("UPDATE licenses SET last_verified_at=? WHERE license_key=?", (now_str(), license_key))
        con.commit()

    return jsonify({
        "status": "valid",
        "message": "License valid",
        "product_code": lic["product_code"],
        "plan": lic["plan"],
        "expiry_date": lic["expiry_date"],
        "customer_name": lic["customer_name"],
        "max_branches": lic["max_branches"],
        "max_employees": lic["max_employees"],
        "server_time": now_str()
    })

@app.route("/admin/license/create", methods=["POST"])
def api_create_license():
    if not check_admin_api():
        return jsonify({"status": "error", "message": "Unauthorized admin key"}), 401

    data = request.get_json(silent=True) or {}
    plan = data.get("plan", "1YEAR")
    if plan not in PLAN_DAYS:
        return jsonify({"status": "error", "message": "Invalid plan"}), 400

    license_key = make_license_key(plan)
    expiry_date = calculate_expiry(plan)

    with db() as con:
        con.execute("""
            INSERT INTO licenses
            (license_key, product_code, plan, customer_name, customer_email, customer_phone, status, issue_date, expiry_date, max_branches, max_employees, notes)
            VALUES (?, ?, ?, ?, ?, ?, 'unused', ?, ?, ?, ?, ?)
        """, (
            license_key,
            PRODUCT_CODE,
            plan,
            data.get("customer_name", ""),
            data.get("customer_email", ""),
            data.get("customer_phone", ""),
            today_str(),
            expiry_date,
            int(data.get("max_branches", 999) or 999),
            int(data.get("max_employees", 999) or 999),
            data.get("notes", "")
        ))
        con.commit()

    return jsonify({"status": "success", "license_key": license_key, "plan": plan, "expiry_date": expiry_date})

@app.route("/admin/licenses")
def api_list_licenses():
    admin_key = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
    if admin_key != ADMIN_KEY:
        return jsonify({"status": "error", "message": "Unauthorized admin key"}), 401

    with db() as con:
        rows = con.execute("SELECT * FROM licenses ORDER BY id DESC").fetchall()
    return jsonify({"status": "success", "licenses": [row_to_dict(r) for r in rows]})

@app.route("/admin/license/reset-device", methods=["POST"])
def api_reset_device():
    if not check_admin_api():
        return jsonify({"status": "error", "message": "Unauthorized admin key"}), 401
    data = request.get_json(silent=True) or {}
    license_key = (data.get("license_key") or "").strip().upper()
    with db() as con:
        con.execute("UPDATE licenses SET device_id=NULL, status='unused', activated_at=NULL WHERE license_key=?", (license_key,))
        con.commit()
    return jsonify({"status": "success", "message": "Device reset completed"})

@app.route("/admin/license/block", methods=["POST"])
def api_block_license():
    if not check_admin_api():
        return jsonify({"status": "error", "message": "Unauthorized admin key"}), 401
    data = request.get_json(silent=True) or {}
    license_key = (data.get("license_key") or "").strip().upper()
    status = data.get("status", "blocked")
    if status not in ("blocked", "active", "unused"):
        return jsonify({"status": "error", "message": "Invalid status"}), 400
    with db() as con:
        con.execute("UPDATE licenses SET status=? WHERE license_key=?", (status, license_key))
        con.commit()
    return jsonify({"status": "success", "message": f"License status changed to {status}"})

ADMIN_TEMPLATE = """
<!doctype html>
<html>
<head>
<title>SmartPay India License Admin</title>
<style>
body{font-family:Arial, sans-serif;background:#f5f7fb;margin:0;padding:24px;color:#0f172a}
.card{background:#fff;border-radius:12px;padding:18px;margin-bottom:18px;box-shadow:0 2px 8px rgba(15,23,42,.08)}
h1,h2{margin-top:0}
input,select,textarea{padding:9px;margin:4px 0 12px;width:100%;box-sizing:border-box;border:1px solid #cbd5e1;border-radius:6px}
button{padding:10px 14px;background:#0b57d0;color:white;border:0;border-radius:6px;cursor:pointer}
table{width:100%;border-collapse:collapse;background:#fff;font-size:13px}
th,td{border-bottom:1px solid #e2e8f0;padding:8px;text-align:left;vertical-align:top}
th{background:#eaf1ff}
.badge{padding:3px 8px;border-radius:999px;background:#e2e8f0}
.actions form{display:inline}
.copy{background:#0f766e}
.danger{background:#b91c1c}
</style>
<script>
function copyText(t){navigator.clipboard.writeText(t); alert("Copied: " + t);}
</script>
</head>
<body>
<div class="card">
<h1>SmartPay India License Admin</h1>
<p>Generate trial, yearly, and lifetime license keys for desktop activation.</p>
<a href="/admin/logout">Logout</a>
</div>

<div class="card">
<h2>Create License</h2>
<form method="post" action="/admin/create">
<label>Customer / Firm Name</label><input name="customer_name" placeholder="Customer firm name">
<label>Email</label><input name="customer_email" placeholder="email">
<label>Phone</label><input name="customer_phone" placeholder="phone">
<label>Plan</label>
<select name="plan">
<option value="TRIAL_30D">Trial - 30 Days</option>
<option value="1YEAR">1 Year</option>
<option value="2YEAR">2 Years</option>
<option value="3YEAR">3 Years</option>
<option value="LIFETIME">Lifetime</option>
</select>
<label>Max Branches</label><input name="max_branches" value="999">
<label>Max Employees</label><input name="max_employees" value="999">
<label>Notes</label><textarea name="notes"></textarea>
<button type="submit">Generate License Key</button>
</form>
</div>

{% if created_key %}
<div class="card">
<h2>Created License Key</h2>
<h3>{{ created_key }}</h3>
<button class="copy" onclick="copyText('{{ created_key }}')">Copy License Key</button>
</div>
{% endif %}

<div class="card">
<h2>All Licenses</h2>
<table>
<tr>
<th>ID</th><th>License Key</th><th>Customer</th><th>Plan</th><th>Status</th><th>Device ID</th><th>Issue</th><th>Expiry</th><th>Last Verify</th><th>Actions</th>
</tr>
{% for l in licenses %}
<tr>
<td>{{ l.id }}</td>
<td><b>{{ l.license_key }}</b><br><button class="copy" onclick="copyText('{{ l.license_key }}')">Copy</button></td>
<td>{{ l.customer_name or '' }}<br>{{ l.customer_phone or '' }}<br>{{ l.customer_email or '' }}</td>
<td>{{ l.plan }}</td>
<td><span class="badge">{{ l.status }}</span></td>
<td>{{ l.device_id or '-' }}</td>
<td>{{ l.issue_date }}</td>
<td>{{ l.expiry_date or 'Lifetime' }}</td>
<td>{{ l.last_verified_at or '-' }}</td>
<td class="actions">
<form method="post" action="/admin/reset-device"><input type="hidden" name="license_key" value="{{ l.license_key }}"><button>Reset Device</button></form>
<form method="post" action="/admin/block"><input type="hidden" name="license_key" value="{{ l.license_key }}"><button class="danger">Block</button></form>
<form method="post" action="/admin/unblock"><input type="hidden" name="license_key" value="{{ l.license_key }}"><button>Unblock</button></form>
</td>
</tr>
{% endfor %}
</table>
</div>
</body>
</html>
"""

LOGIN_TEMPLATE = """
<!doctype html>
<html>
<head>
<title>SmartPay India Admin Login</title>
<style>
body{font-family:Arial,sans-serif;background:#f5f7fb;margin:0;padding:80px;color:#0f172a}
.card{max-width:420px;margin:auto;background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 8px rgba(15,23,42,.08)}
input{padding:10px;width:100%;box-sizing:border-box;margin:8px 0 14px;border:1px solid #cbd5e1;border-radius:6px}
button{padding:10px 14px;background:#0b57d0;color:white;border:0;border-radius:6px;cursor:pointer}
.err{color:#b91c1c}
</style>
</head>
<body>
<div class="card">
<h1>SmartPay India Admin</h1>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
<form method="post">
<label>Admin Password</label>
<input type="password" name="password" autofocus>
<button type="submit">Login</button>
</form>
</div>
</body>
</html>
"""

@app.route("/admin", methods=["GET"])
@login_required
def admin_home():
    with db() as con:
        rows = con.execute("SELECT * FROM licenses ORDER BY id DESC").fetchall()
    return render_template_string(ADMIN_TEMPLATE, licenses=rows, created_key=request.args.get("created_key"))

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_home"))
        return render_template_string(LOGIN_TEMPLATE, error="Invalid password")
    return render_template_string(LOGIN_TEMPLATE, error=None)

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))

@app.route("/admin/create", methods=["POST"])
@login_required
def admin_create():
    plan = request.form.get("plan", "1YEAR")
    if plan not in PLAN_DAYS:
        plan = "1YEAR"
    license_key = make_license_key(plan)
    expiry_date = calculate_expiry(plan)
    with db() as con:
        con.execute("""
            INSERT INTO licenses
            (license_key, product_code, plan, customer_name, customer_email, customer_phone, status, issue_date, expiry_date, max_branches, max_employees, notes)
            VALUES (?, ?, ?, ?, ?, ?, 'unused', ?, ?, ?, ?, ?)
        """, (
            license_key,
            PRODUCT_CODE,
            plan,
            request.form.get("customer_name", ""),
            request.form.get("customer_email", ""),
            request.form.get("customer_phone", ""),
            today_str(),
            expiry_date,
            int(request.form.get("max_branches", 999) or 999),
            int(request.form.get("max_employees", 999) or 999),
            request.form.get("notes", "")
        ))
        con.commit()
    return redirect(url_for("admin_home", created_key=license_key))

@app.route("/admin/reset-device", methods=["POST"])
@login_required
def admin_reset_device():
    license_key = request.form.get("license_key", "").strip().upper()
    with db() as con:
        con.execute("UPDATE licenses SET device_id=NULL, status='unused', activated_at=NULL WHERE license_key=?", (license_key,))
        con.commit()
    return redirect(url_for("admin_home"))

@app.route("/admin/block", methods=["POST"])
@login_required
def admin_block():
    license_key = request.form.get("license_key", "").strip().upper()
    with db() as con:
        con.execute("UPDATE licenses SET status='blocked' WHERE license_key=?", (license_key,))
        con.commit()
    return redirect(url_for("admin_home"))

@app.route("/admin/unblock", methods=["POST"])
@login_required
def admin_unblock():
    license_key = request.form.get("license_key", "").strip().upper()
    with db() as con:
        con.execute("UPDATE licenses SET status='active' WHERE license_key=?", (license_key,))
        con.commit()
    return redirect(url_for("admin_home"))

if __name__ == "__main__":
    app.run(debug=True)
