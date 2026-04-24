from flask import Flask, request, jsonify, redirect, url_for, session, render_template_string
import os
import sqlite3
import datetime
import secrets
import string

APP_NAME = "SmartPay India"
PRODUCT_CODE = "SMARTPAY_INDIA"
DB_PATH = os.environ.get("LICENSE_DB_PATH", "licenses.db")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

PLAN_DAYS = {
    "TRIAL_30D": 30,
    "1Y": 365,
    "2Y": 730,
    "3Y": 1095,
    "LIFETIME": None,
}

PLAN_PREFIX = {
    "TRIAL_30D": "SMARTPAY-TRIAL-30D",
    "1Y": "SMARTPAY-1Y",
    "2Y": "SMARTPAY-2Y",
    "3Y": "SMARTPAY-3Y",
    "LIFETIME": "SMARTPAY-LIFE",
}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT UNIQUE NOT NULL,
                product_code TEXT NOT NULL DEFAULT 'SMARTPAY_INDIA',
                customer_name TEXT DEFAULT '',
                customer_email TEXT DEFAULT '',
                customer_phone TEXT DEFAULT '',
                plan TEXT NOT NULL,
                expiry_date TEXT,
                device_id TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                activated_at TEXT,
                last_verified_at TEXT,
                max_branches INTEGER DEFAULT 0,
                max_employees INTEGER DEFAULT 0,
                notes TEXT DEFAULT ''
            )
        """)
        conn.commit()


@app.before_request
def before_request():
    init_db()


def now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def today_date():
    return datetime.date.today()


def make_key(plan):
    prefix = PLAN_PREFIX.get(plan, "SMARTPAY-LIC")
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    parts = []
    for _ in range(3):
        parts.append("".join(secrets.choice(alphabet) for _ in range(4)))
    return prefix + "-" + "-".join(parts)


def compute_expiry(plan):
    days = PLAN_DAYS.get(plan)
    if days is None:
        return None
    return (today_date() + datetime.timedelta(days=days)).isoformat()


def is_admin():
    return session.get("admin_logged_in") is True


def require_admin_api():
    if ADMIN_KEY:
        supplied = request.headers.get("X-Admin-Key") or request.args.get("admin_key")
        return supplied == ADMIN_KEY
    return False


def normalize_device_id(data):
    return (
        data.get("device_id")
        or data.get("customer_id")
        or data.get("machine_id")
        or data.get("computer_id")
        or ""
    ).strip()


def license_row_to_dict(row):
    return {
        "id": row["id"],
        "license_key": row["license_key"],
        "product_code": row["product_code"],
        "customer_name": row["customer_name"],
        "customer_email": row["customer_email"],
        "customer_phone": row["customer_phone"],
        "plan": row["plan"],
        "expiry_date": row["expiry_date"],
        "device_id": row["device_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "activated_at": row["activated_at"],
        "last_verified_at": row["last_verified_at"],
        "max_branches": row["max_branches"],
        "max_employees": row["max_employees"],
        "notes": row["notes"],
    }


def check_expired(row):
    if row["plan"] == "LIFETIME" or not row["expiry_date"]:
        return False
    try:
        return today_date() > datetime.datetime.strptime(row["expiry_date"], "%Y-%m-%d").date()
    except Exception:
        return True


@app.route("/")
def home():
    return f"{APP_NAME} License API Running"


@app.route("/health")
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": APP_NAME, "product_code": PRODUCT_CODE})


@app.route("/api/license/activate", methods=["POST"])
@app.route("/license/activate", methods=["POST"])
@app.route("/activate-license", methods=["POST"])
@app.route("/activate", methods=["POST"])
def activate_license():
    data = request.get_json(silent=True) or {}
    license_key = (data.get("license_key") or "").strip().upper()
    device_id = normalize_device_id(data)
    firm_name = (data.get("firm_name") or data.get("customer_name") or "").strip()
    product_code = (data.get("product_code") or PRODUCT_CODE).strip().upper()

    if not license_key:
        return jsonify({"status": "error", "message": "License key is required"}), 400
    if not device_id:
        return jsonify({"status": "error", "message": "Customer ID / Device ID is required"}), 400
    if product_code != PRODUCT_CODE:
        return jsonify({"status": "error", "message": "Invalid product code"}), 400

    with db() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,)).fetchone()
        if not row:
            return jsonify({"status": "error", "message": "Invalid license key"}), 400

        if row["status"] == "blocked":
            return jsonify({"status": "error", "message": "License is blocked"}), 403

        if check_expired(row):
            return jsonify({"status": "expired", "message": "License expired", "expiry_date": row["expiry_date"]}), 403

        if row["device_id"] and row["device_id"] != device_id:
            return jsonify({"status": "error", "message": "License already activated on another computer"}), 403

        if not row["device_id"]:
            conn.execute(
                """UPDATE licenses
                   SET device_id=?, activated_at=?, last_verified_at=?,
                       customer_name=CASE WHEN customer_name='' THEN ? ELSE customer_name END
                   WHERE id=?""",
                (device_id, now_iso(), now_iso(), firm_name, row["id"]),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM licenses WHERE id = ?", (row["id"],)).fetchone()
            msg = "License activated successfully"
        else:
            conn.execute("UPDATE licenses SET last_verified_at=? WHERE id=?", (now_iso(), row["id"]))
            conn.commit()
            msg = "License already activated on this computer"

        return jsonify({
            "status": "success",
            "message": msg,
            "license_key": row["license_key"],
            "plan": row["plan"],
            "expiry_date": row["expiry_date"],
            "customer_name": row["customer_name"],
            "product_code": row["product_code"],
        })


@app.route("/api/license/verify", methods=["POST"])
@app.route("/license/verify", methods=["POST"])
@app.route("/verify-license", methods=["POST"])
@app.route("/validate", methods=["POST"])
def verify_license():
    data = request.get_json(silent=True) or {}
    license_key = (data.get("license_key") or "").strip().upper()
    device_id = normalize_device_id(data)
    product_code = (data.get("product_code") or PRODUCT_CODE).strip().upper()

    if not license_key or not device_id:
        return jsonify({"status": "error", "message": "License key and Customer ID are required"}), 400
    if product_code != PRODUCT_CODE:
        return jsonify({"status": "error", "message": "Invalid product code"}), 400

    with db() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,)).fetchone()
        if not row:
            return jsonify({"status": "error", "message": "Invalid license key"}), 400
        if row["status"] == "blocked":
            return jsonify({"status": "error", "message": "License is blocked"}), 403
        if row["device_id"] != device_id:
            return jsonify({"status": "error", "message": "Unauthorized computer"}), 403
        if check_expired(row):
            return jsonify({"status": "expired", "message": "License expired", "expiry_date": row["expiry_date"]}), 403
        conn.execute("UPDATE licenses SET last_verified_at=? WHERE id=?", (now_iso(), row["id"]))
        conn.commit()
        return jsonify({
            "status": "valid",
            "message": "License valid",
            "license_key": row["license_key"],
            "plan": row["plan"],
            "expiry_date": row["expiry_date"],
            "customer_name": row["customer_name"],
            "product_code": row["product_code"],
        })


ADMIN_TEMPLATE = """
<!doctype html>
<html>
<head>
  <title>SmartPay India License Admin</title>
  <style>
    body{font-family:Arial, sans-serif; margin:30px; background:#f7f9fc; color:#172033}
    .card{background:white; border:1px solid #d9e1ec; border-radius:10px; padding:18px; margin-bottom:18px; box-shadow:0 2px 8px rgba(0,0,0,.04)}
    h1{margin:0 0 6px; color:#0f56b3}
    input,select,textarea{padding:8px; margin:5px 0; width:100%; box-sizing:border-box}
    button{padding:8px 12px; margin:3px; cursor:pointer}
    table{width:100%; border-collapse:collapse; background:white}
    th,td{border:1px solid #d9e1ec; padding:8px; font-size:13px; vertical-align:top}
    th{background:#e9eef6}
    .row{display:grid; grid-template-columns:repeat(4,1fr); gap:10px}
    .key{font-weight:bold; color:#0f56b3}
    .blocked{color:#b00020; font-weight:bold}
    .active{color:#0a7a2f; font-weight:bold}
    a{color:#0f56b3}
  </style>
</head>
<body>
  {% if not logged_in %}
    <div class="card" style="max-width:420px;margin:80px auto;">
      <h1>SmartPay India</h1>
      <h3>License Admin Login</h3>
      {% if error %}<p style="color:red">{{error}}</p>{% endif %}
      <form method="post" action="/admin/login">
        <label>Admin Password</label>
        <input type="password" name="password" autofocus>
        <button type="submit">Login</button>
      </form>
    </div>
  {% else %}
    <h1>SmartPay India License Admin</h1>
    <p><a href="/admin/logout">Logout</a> | <a href="/health">Health</a></p>

    {% if message %}
      <div class="card"><b>{{message}}</b></div>
    {% endif %}

    <div class="card">
      <h3>Create New License</h3>
      <form method="post" action="/admin/license/create">
        <div class="row">
          <div><label>Customer / Firm Name</label><input name="customer_name" placeholder="Customer firm name"></div>
          <div><label>Phone</label><input name="customer_phone" placeholder="Phone"></div>
          <div><label>Email</label><input name="customer_email" placeholder="Email"></div>
          <div>
            <label>Plan</label>
            <select name="plan">
              <option value="TRIAL_30D">Trial 30 Days</option>
              <option value="1Y">1 Year</option>
              <option value="2Y">2 Years</option>
              <option value="3Y">3 Years</option>
              <option value="LIFETIME">Lifetime</option>
            </select>
          </div>
        </div>
        <div class="row">
          <div><label>Max Branches</label><input name="max_branches" value="0"></div>
          <div><label>Max Employees</label><input name="max_employees" value="0"></div>
          <div><label>Manual Expiry Date (optional YYYY-MM-DD)</label><input name="expiry_date" placeholder="Leave blank for auto"></div>
          <div><label>Notes</label><input name="notes" placeholder="Optional"></div>
        </div>
        <button type="submit">Generate License Key</button>
      </form>
    </div>

    <div class="card">
      <h3>Licenses</h3>
      <table>
        <tr>
          <th>ID</th><th>License Key</th><th>Customer</th><th>Plan</th><th>Expiry</th>
          <th>Device ID</th><th>Status</th><th>Created</th><th>Activated</th><th>Last Check</th><th>Action</th>
        </tr>
        {% for l in licenses %}
        <tr>
          <td>{{l.id}}</td>
          <td class="key">{{l.license_key}}</td>
          <td>{{l.customer_name}}<br>{{l.customer_phone}}<br>{{l.customer_email}}</td>
          <td>{{l.plan}}</td>
          <td>{{l.expiry_date or "Lifetime"}}</td>
          <td style="max-width:260px;word-break:break-all">{{l.device_id or "-"}}</td>
          <td class="{{l.status}}">{{l.status}}</td>
          <td>{{l.created_at}}</td>
          <td>{{l.activated_at or "-"}}</td>
          <td>{{l.last_verified_at or "-"}}</td>
          <td>
            <form method="post" action="/admin/license/{{l.id}}/reset-device" style="display:inline"><button>Reset Device</button></form>
            {% if l.status == "blocked" %}
              <form method="post" action="/admin/license/{{l.id}}/unblock" style="display:inline"><button>Unblock</button></form>
            {% else %}
              <form method="post" action="/admin/license/{{l.id}}/block" style="display:inline"><button>Block</button></form>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </table>
    </div>
  {% endif %}
</body>
</html>
"""


@app.route("/admin", methods=["GET"])
def admin_home():
    if not is_admin():
        return render_template_string(ADMIN_TEMPLATE, logged_in=False, error=None)
    with db() as conn:
        rows = conn.execute("SELECT * FROM licenses ORDER BY id DESC").fetchall()
    return render_template_string(ADMIN_TEMPLATE, logged_in=True, licenses=rows, message=request.args.get("message"))


@app.route("/admin/login", methods=["POST"])
def admin_login():
    password = request.form.get("password", "")
    if password == ADMIN_PASSWORD:
        session["admin_logged_in"] = True
        return redirect(url_for("admin_home"))
    return render_template_string(ADMIN_TEMPLATE, logged_in=False, error="Wrong admin password")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_home"))


@app.route("/admin/create", methods=["POST"])
@app.route("/admin/license/create", methods=["POST"])
def admin_create_license():
    if not is_admin() and not require_admin_api():
        return redirect(url_for("admin_home"))

    data = request.form if request.form else (request.get_json(silent=True) or {})
    plan = data.get("plan", "1Y")
    if plan not in PLAN_DAYS:
        plan = "1Y"
    key = make_key(plan)
    expiry_date = (data.get("expiry_date") or "").strip() or compute_expiry(plan)

    try:
        max_branches = int(data.get("max_branches") or 0)
    except Exception:
        max_branches = 0
    try:
        max_employees = int(data.get("max_employees") or 0)
    except Exception:
        max_employees = 0

    with db() as conn:
        conn.execute(
            """INSERT INTO licenses
               (license_key, product_code, customer_name, customer_email, customer_phone, plan, expiry_date,
                status, created_at, max_branches, max_employees, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
            (
                key, PRODUCT_CODE,
                data.get("customer_name", ""),
                data.get("customer_email", ""),
                data.get("customer_phone", ""),
                plan, expiry_date, now_iso(), max_branches, max_employees, data.get("notes", ""),
            ),
        )
        conn.commit()

    if request.is_json or require_admin_api():
        return jsonify({"status": "success", "license_key": key, "plan": plan, "expiry_date": expiry_date})
    return redirect(url_for("admin_home", message=f"Created license key: {key}"))


@app.route("/admin/license/<int:license_id>/reset-device", methods=["POST"])
def admin_reset_device(license_id):
    if not is_admin():
        return redirect(url_for("admin_home"))
    with db() as conn:
        conn.execute("UPDATE licenses SET device_id=NULL, activated_at=NULL WHERE id=?", (license_id,))
        conn.commit()
    return redirect(url_for("admin_home", message="Device reset completed"))


@app.route("/admin/license/<int:license_id>/block", methods=["POST"])
def admin_block(license_id):
    if not is_admin():
        return redirect(url_for("admin_home"))
    with db() as conn:
        conn.execute("UPDATE licenses SET status='blocked' WHERE id=?", (license_id,))
        conn.commit()
    return redirect(url_for("admin_home", message="License blocked"))


@app.route("/admin/license/<int:license_id>/unblock", methods=["POST"])
def admin_unblock(license_id):
    if not is_admin():
        return redirect(url_for("admin_home"))
    with db() as conn:
        conn.execute("UPDATE licenses SET status='active' WHERE id=?", (license_id,))
        conn.commit()
    return redirect(url_for("admin_home", message="License unblocked"))


@app.route("/admin/licenses.json")
def admin_licenses_json():
    if not require_admin_api() and not is_admin():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    with db() as conn:
        rows = conn.execute("SELECT * FROM licenses ORDER BY id DESC").fetchall()
    return jsonify({"status": "success", "licenses": [license_row_to_dict(r) for r in rows]})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
