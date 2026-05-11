from flask import Flask, request, jsonify, redirect, url_for, session, render_template_string
import os
import datetime
import secrets
import base64
import hashlib
import psycopg2
import psycopg2.extras

try:
    from google.cloud import storage
except Exception:
    storage = None

try:
    from cryptography.fernet import Fernet
except Exception:
    Fernet = None


APP_NAME = "SmartPay India"
PRODUCT_CODE = "SMARTPAY_INDIA"

DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "")
BACKUP_ENCRYPTION_KEY = os.environ.get("BACKUP_ENCRYPTION_KEY", "")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")


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
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS licenses (
                    id SERIAL PRIMARY KEY,
                    license_key TEXT UNIQUE NOT NULL,
                    product_code TEXT NOT NULL DEFAULT 'SMARTPAY_INDIA',
                    customer_name TEXT DEFAULT '',
                    customer_email TEXT DEFAULT '',
                    customer_phone TEXT DEFAULT '',
                    plan TEXT NOT NULL,
                    expiry_date DATE,
                    device_id TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    last_verified_at TEXT,
                    max_branches INTEGER DEFAULT 0,
                    max_employees INTEGER DEFAULT 0,
                    notes TEXT DEFAULT ''
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cloud_backups (
                    id SERIAL PRIMARY KEY,
                    license_key TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    product_code TEXT NOT NULL DEFAULT 'SMARTPAY_INDIA',
                    firm_name TEXT DEFAULT '',
                    app_version TEXT DEFAULT '',
                    backup_time TEXT NOT NULL,
                    object_name TEXT NOT NULL,
                    original_filename TEXT DEFAULT '',
                    backup_size BIGINT DEFAULT 0,
                    checksum_sha256 TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active'
                )
                """
            )
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


def row_value(row, key):
    value = row.get(key)
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


def license_row_to_dict(row):
    return {
        "id": row_value(row, "id"),
        "license_key": row_value(row, "license_key"),
        "product_code": row_value(row, "product_code"),
        "customer_name": row_value(row, "customer_name"),
        "customer_email": row_value(row, "customer_email"),
        "customer_phone": row_value(row, "customer_phone"),
        "plan": row_value(row, "plan"),
        "expiry_date": row_value(row, "expiry_date"),
        "device_id": row_value(row, "device_id"),
        "status": row_value(row, "status"),
        "created_at": row_value(row, "created_at"),
        "activated_at": row_value(row, "activated_at"),
        "last_verified_at": row_value(row, "last_verified_at"),
        "max_branches": row_value(row, "max_branches"),
        "max_employees": row_value(row, "max_employees"),
        "notes": row_value(row, "notes"),
    }


def check_expired(row):
    if row["plan"] == "LIFETIME" or not row.get("expiry_date"):
        return False
    try:
        expiry = row["expiry_date"]
        if isinstance(expiry, str):
            expiry = datetime.datetime.strptime(expiry, "%Y-%m-%d").date()
        return today_date() > expiry
    except Exception:
        return True


def _backup_cipher():
    if Fernet is None:
        raise RuntimeError("cryptography package is not installed")
    key = os.environ.get("BACKUP_ENCRYPTION_KEY") or BACKUP_ENCRYPTION_KEY
    if not key:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is not configured")
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _gcs_bucket():
    if storage is None:
        raise RuntimeError("google-cloud-storage package is not installed")
    bucket_name = os.environ.get("GCS_BUCKET_NAME") or GCS_BUCKET_NAME
    if not bucket_name:
        raise RuntimeError("GCS_BUCKET_NAME is not configured")
    client = storage.Client()
    return client.bucket(bucket_name)


def _verify_active_license(license_key, device_id):
    license_key = (license_key or "").strip().upper()
    device_id = (device_id or "").strip().upper()

    if not license_key or not device_id:
        return None, ("License key and device ID are required", 400)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM licenses WHERE license_key=%s", (license_key,))
            row = cur.fetchone()

    if not row:
        return None, ("Invalid license key", 400)
    if row["status"] == "blocked":
        return None, ("License is blocked", 403)
    if check_expired(row):
        return None, ("License expired", 403)

    saved_device = (row.get("device_id") or "").strip().upper()
    if saved_device and saved_device != device_id:
        return None, ("License is activated on another device", 403)
    if not saved_device:
        return None, ("License is not activated yet", 403)

    return row, None


def _safe_object_part(value):
    raw = str(value or "").strip().replace("\\", "_").replace("/", "_")
    keep = []
    for ch in raw:
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep)[:80] or "item"


def _bool_from_env(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on")


@app.route("/")
def home():
    return f"{APP_NAME} License API Running - PostgreSQL"


@app.route("/health")
@app.route("/api/health")
def health():
    try:
        init_db()
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"

    return jsonify(
        {
            "status": "ok",
            "app": APP_NAME,
            "product_code": PRODUCT_CODE,
            "database": "postgresql",
            "db_status": db_status,
            "backup": {
                "gcs_bucket": bool(os.environ.get("GCS_BUCKET_NAME") or GCS_BUCKET_NAME),
                "encryption_key": bool(os.environ.get("BACKUP_ENCRYPTION_KEY") or BACKUP_ENCRYPTION_KEY),
            },
        }
    )


# -------------------------------------------------------------------
# SmartPay Desktop Update Check API - Standard / Silver / Gold / Platinum / Customer Channels
#
# Supported URLs:
#   /smartpay-standard/latest-version
#   /smartpay-standard/latest-version?channel=platinum
#   /smartpay-standard/platinum/latest-version
#   /api/smartpay-standard/latest-version?channel=platinum
#
# Render environment variable examples:
#   SMARTPAY_STANDARD_LATEST_VERSION
#   SMARTPAY_STANDARD_DOWNLOAD_URL
#   SMARTPAY_STANDARD_RELEASE_NOTES
#   SMARTPAY_STANDARD_FORCE_UPDATE
#   SMARTPAY_STANDARD_SHA256
#
#   SMARTPAY_PLATINUM_LATEST_VERSION
#   SMARTPAY_PLATINUM_DOWNLOAD_URL
#   SMARTPAY_PLATINUM_RELEASE_NOTES
#   SMARTPAY_PLATINUM_FORCE_UPDATE
#   SMARTPAY_PLATINUM_SHA256
#
# For customer_devi channel:
#   SMARTPAY_CUSTOMER_DEVI_LATEST_VERSION
#   SMARTPAY_CUSTOMER_DEVI_DOWNLOAD_URL
#   SMARTPAY_CUSTOMER_DEVI_RELEASE_NOTES
#   SMARTPAY_CUSTOMER_DEVI_FORCE_UPDATE
#   SMARTPAY_CUSTOMER_DEVI_SHA256
# -------------------------------------------------------------------

ALLOWED_UPDATE_CHANNELS = {
    "standard": "SMARTPAY_STANDARD",
    "silver": "SMARTPAY_SILVER",
    "gold": "SMARTPAY_GOLD",
    "platinum": "SMARTPAY_PLATINUM",
}


def _normalize_update_channel(raw_channel):
    channel = str(raw_channel or "standard").strip().lower()
    channel = channel.replace("-", "_").replace(" ", "_")
    # Allow simple customer-specific channels like customer_devi.
    safe = "".join(ch for ch in channel if ch.isalnum() or ch == "_")
    return safe or "standard"


def _update_env_prefix(channel):
    channel = _normalize_update_channel(channel)
    if channel in ALLOWED_UPDATE_CHANNELS:
        return ALLOWED_UPDATE_CHANNELS[channel], channel
    if channel.startswith("customer_"):
        return "SMARTPAY_" + channel.upper(), channel
    # Unknown channel falls back safely to standard.
    return "SMARTPAY_STANDARD", "standard"


def _latest_version_payload(channel=None):
    prefix, channel = _update_env_prefix(channel)

    latest_version = os.environ.get(f"{prefix}_LATEST_VERSION", "")
    download_url = os.environ.get(f"{prefix}_DOWNLOAD_URL", "")
    release_notes = os.environ.get(f"{prefix}_RELEASE_NOTES", "")
    force_update = _bool_from_env(os.environ.get(f"{prefix}_FORCE_UPDATE", "false"))
    sha256 = os.environ.get(f"{prefix}_SHA256", "")

    # Backward-compatible fallback to old Standard env vars.
    if not latest_version and channel != "standard":
        latest_version = os.environ.get("SMARTPAY_STANDARD_LATEST_VERSION", "1.0.3")

    package_edition = os.environ.get(f"{prefix}_PACKAGE_EDITION", channel.upper())

    return {
        "ok": True,
        "product": f"SmartPay India {package_edition.title()}",
        "package_edition": package_edition,
        "channel": channel,
        "latest_version": latest_version or "1.0.3",
        "download_url": download_url,
        "release_notes": release_notes,
        "force_update": force_update,
        "sha256": sha256,
    }


@app.route("/smartpay-standard/latest-version", methods=["GET"])
@app.route("/api/smartpay-standard/latest-version", methods=["GET"])
def smartpay_standard_latest_version():
    channel = request.args.get("channel") or request.args.get("update_channel") or "standard"
    return jsonify(_latest_version_payload(channel))


@app.route("/smartpay-standard/<channel>/latest-version", methods=["GET"])
@app.route("/api/smartpay-standard/<channel>/latest-version", methods=["GET"])
def smartpay_standard_latest_version_by_channel(channel):
    return jsonify(_latest_version_payload(channel))


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
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM licenses WHERE license_key = %s", (license_key,))
            row = cur.fetchone()

            if not row:
                return jsonify({"status": "error", "message": "Invalid license key"}), 400
            if row["status"] == "blocked":
                return jsonify({"status": "error", "message": "License is blocked"}), 403
            if check_expired(row):
                return (
                    jsonify(
                        {
                            "status": "expired",
                            "message": "License expired",
                            "expiry_date": row_value(row, "expiry_date"),
                        }
                    ),
                    403,
                )
            if row.get("device_id") and row["device_id"] != device_id:
                return jsonify({"status": "error", "message": "License already activated on another computer"}), 403

            if not row.get("device_id"):
                cur.execute(
                    """
                    UPDATE licenses
                    SET device_id=%s,
                        activated_at=%s,
                        last_verified_at=%s,
                        customer_name=CASE WHEN COALESCE(customer_name, '')='' THEN %s ELSE customer_name END
                    WHERE id=%s
                    """,
                    (device_id, now_iso(), now_iso(), firm_name, row["id"]),
                )
                conn.commit()
                cur.execute("SELECT * FROM licenses WHERE id = %s", (row["id"],))
                row = cur.fetchone()
                msg = "License activated successfully"
            else:
                cur.execute("UPDATE licenses SET last_verified_at=%s WHERE id=%s", (now_iso(), row["id"]))
                conn.commit()
                msg = "License already activated on this computer"

    return jsonify(
        {
            "status": "success",
            "message": msg,
            "license_key": row["license_key"],
            "plan": row["plan"],
            "expiry_date": row_value(row, "expiry_date"),
            "customer_name": row.get("customer_name"),
            "product_code": row["product_code"],
        }
    )


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
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM licenses WHERE license_key = %s", (license_key,))
            row = cur.fetchone()

            if not row:
                return jsonify({"status": "error", "message": "Invalid license key"}), 400
            if row["status"] == "blocked":
                return jsonify({"status": "error", "message": "License is blocked"}), 403
            if row.get("device_id") != device_id:
                return jsonify({"status": "error", "message": "Unauthorized computer"}), 403
            if check_expired(row):
                return (
                    jsonify(
                        {
                            "status": "expired",
                            "message": "License expired",
                            "expiry_date": row_value(row, "expiry_date"),
                        }
                    ),
                    403,
                )

            cur.execute("UPDATE licenses SET last_verified_at=%s WHERE id=%s", (now_iso(), row["id"]))
            conn.commit()

    return jsonify(
        {
            "status": "valid",
            "message": "License valid",
            "license_key": row["license_key"],
            "plan": row["plan"],
            "expiry_date": row_value(row, "expiry_date"),
            "customer_name": row.get("customer_name"),
            "product_code": row["product_code"],
        }
    )


ADMIN_TEMPLATE = """
<!doctype html>
<html>
<head>
    <title>SmartPay India License Admin</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f6f8fb; color: #222; }
        h1, h3 { color: #0b4f9c; }
        input, select, textarea { padding: 7px; margin: 4px 0; width: 100%; max-width: 420px; }
        button { padding: 7px 12px; margin: 2px; cursor: pointer; }
        table { border-collapse: collapse; width: 100%; background: white; font-size: 13px; }
        th, td { border: 1px solid #ddd; padding: 7px; vertical-align: top; }
        th { background: #0b4f9c; color: white; }
        .card { background: white; padding: 16px; border: 1px solid #ddd; border-radius: 8px; margin-bottom: 18px; }
        .msg { background: #e8f6ec; color: #146c2e; padding: 10px; margin-bottom: 12px; border-radius: 6px; }
        .err { background: #ffecec; color: #b00020; padding: 10px; margin-bottom: 12px; border-radius: 6px; }
        .inline-form { display:inline; }
    </style>
</head>
<body>
{% if not logged_in %}
    <div class="card">
        <h1>SmartPay India</h1>
        <h3>License Admin Login</h3>
        {% if error %}<div class="err">{{ error }}</div>{% endif %}
        <form method="post" action="{{ url_for('admin_login') }}">
            <input type="password" name="password" placeholder="Admin Password" required>
            <br>
            <button type="submit">Login</button>
        </form>
    </div>
{% else %}
    <h1>SmartPay India License Admin</h1>
    <p><a href="{{ url_for('admin_logout') }}">Logout</a> | <a href="{{ url_for('health') }}">Health</a></p>
    {% if message %}<div class="msg">{{ message }}</div>{% endif %}

    <div class="card">
        <h3>Create New License</h3>
        <form method="post" action="{{ url_for('admin_create_license') }}">
            <label>Customer / Firm Name</label><br>
            <input name="customer_name"><br>
            <label>Phone</label><br>
            <input name="customer_phone"><br>
            <label>Email</label><br>
            <input name="customer_email"><br>
            <label>Plan</label><br>
            <select name="plan">
                <option value="TRIAL_30D">Trial 30 Days</option>
                <option value="1Y" selected>1 Year</option>
                <option value="2Y">2 Years</option>
                <option value="3Y">3 Years</option>
                <option value="LIFETIME">Lifetime</option>
            </select><br>
            <label>Max Branches</label><br>
            <input name="max_branches" value="0"><br>
            <label>Max Employees</label><br>
            <input name="max_employees" value="0"><br>
            <label>Manual Expiry Date (optional YYYY-MM-DD)</label><br>
            <input name="expiry_date"><br>
            <label>Notes</label><br>
            <textarea name="notes"></textarea><br>
            <button type="submit">Generate License Key</button>
        </form>
    </div>

    <h3>Licenses</h3>
    <table>
        <tr>
            <th>ID</th>
            <th>License Key</th>
            <th>Customer</th>
            <th>Plan</th>
            <th>Expiry</th>
            <th>Device ID</th>
            <th>Status</th>
            <th>Created</th>
            <th>Activated</th>
            <th>Last Check</th>
            <th>Action</th>
        </tr>
        {% for l in licenses %}
        <tr>
            <td>{{ l.id }}</td>
            <td>{{ l.license_key }}</td>
            <td>{{ l.customer_name }}<br>{{ l.customer_phone }}<br>{{ l.customer_email }}</td>
            <td>{{ l.plan }}</td>
            <td>{{ l.expiry_date or "Lifetime" }}</td>
            <td>{{ l.device_id or "-" }}</td>
            <td>{{ l.status }}</td>
            <td>{{ l.created_at }}</td>
            <td>{{ l.activated_at or "-" }}</td>
            <td>{{ l.last_verified_at or "-" }}</td>
            <td>
                <form class="inline-form" method="post" action="{{ url_for('admin_reset_device', license_id=l.id) }}">
                    <button type="submit">Reset Device</button>
                </form>
                {% if l.status == "blocked" %}
                <form class="inline-form" method="post" action="{{ url_for('admin_unblock', license_id=l.id) }}">
                    <button type="submit">Unblock</button>
                </form>
                {% else %}
                <form class="inline-form" method="post" action="{{ url_for('admin_block', license_id=l.id) }}">
                    <button type="submit">Block</button>
                </form>
                {% endif %}
            </td>
        </tr>
        {% endfor %}
    </table>
{% endif %}
</body>
</html>
"""


@app.route("/admin", methods=["GET"])
def admin_home():
    if not is_admin():
        return render_template_string(ADMIN_TEMPLATE, logged_in=False, error=None)

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM licenses ORDER BY id DESC")
            rows = cur.fetchall()

    return render_template_string(
        ADMIN_TEMPLATE,
        logged_in=True,
        licenses=rows,
        message=request.args.get("message"),
    )


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
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO licenses (
                    license_key, product_code, customer_name, customer_email, customer_phone,
                    plan, expiry_date, status, created_at, max_branches, max_employees, notes
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s, %s)
                """,
                (
                    key,
                    PRODUCT_CODE,
                    data.get("customer_name", ""),
                    data.get("customer_email", ""),
                    data.get("customer_phone", ""),
                    plan,
                    expiry_date,
                    now_iso(),
                    max_branches,
                    max_employees,
                    data.get("notes", ""),
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
        with conn.cursor() as cur:
            cur.execute("UPDATE licenses SET device_id=NULL, activated_at=NULL WHERE id=%s", (license_id,))
        conn.commit()

    return redirect(url_for("admin_home", message="Device reset completed"))


@app.route("/admin/license/<int:license_id>/block", methods=["POST"])
def admin_block(license_id):
    if not is_admin():
        return redirect(url_for("admin_home"))

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE licenses SET status='blocked' WHERE id=%s", (license_id,))
        conn.commit()

    return redirect(url_for("admin_home", message="License blocked"))


@app.route("/admin/license/<int:license_id>/unblock", methods=["POST"])
def admin_unblock(license_id):
    if not is_admin():
        return redirect(url_for("admin_home"))

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE licenses SET status='active' WHERE id=%s", (license_id,))
        conn.commit()

    return redirect(url_for("admin_home", message="License unblocked"))


@app.route("/admin/licenses.json")
def admin_licenses_json():
    if not require_admin_api() and not is_admin():
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM licenses ORDER BY id DESC")
            rows = cur.fetchall()

    return jsonify({"status": "success", "licenses": [license_row_to_dict(r) for r in rows]})


@app.route("/api/backup/upload", methods=["POST"])
@app.route("/backup/upload", methods=["POST"])
def backup_upload():
    data = request.get_json(silent=True) or {}
    license_key = (data.get("license_key") or "").strip().upper()
    device_id = normalize_device_id(data)
    product_code = (data.get("product_code") or PRODUCT_CODE).strip().upper()
    firm_name = (data.get("firm_name") or "").strip()
    app_version = (data.get("app_version") or "").strip()
    filename = (data.get("filename") or "smartpay-backup.zip").strip()
    backup_b64 = data.get("backup_base64") or ""

    if product_code != PRODUCT_CODE:
        return jsonify({"ok": False, "status": "error", "message": "Invalid product code"}), 400

    lic, err = _verify_active_license(license_key, device_id)
    if err:
        msg, code = err
        return jsonify({"ok": False, "status": "error", "message": msg}), code

    if not backup_b64:
        return jsonify({"ok": False, "status": "error", "message": "Backup file is missing"}), 400

    try:
        raw = base64.b64decode(backup_b64.encode("utf-8"))
        checksum = hashlib.sha256(raw).hexdigest()
        encrypted = _backup_cipher().encrypt(raw)
        ts = now_iso()

        safe_key = _safe_object_part(license_key)
        safe_device = _safe_object_part(device_id)
        object_name = (
            f"smartpay/{safe_key}/{safe_device}/"
            f"{ts.replace(':','').replace('-','').replace('Z','')}_{_safe_object_part(filename)}.enc"
        )

        bucket = _gcs_bucket()
        blob = bucket.blob(object_name)
        blob.upload_from_string(encrypted, content_type="application/octet-stream")

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cloud_backups(
                        license_key, device_id, product_code, firm_name, app_version,
                        backup_time, object_name, original_filename, backup_size,
                        checksum_sha256, status
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')
                    RETURNING id
                    """,
                    (
                        license_key,
                        device_id,
                        product_code,
                        firm_name,
                        app_version,
                        ts,
                        object_name,
                        filename,
                        len(raw),
                        checksum,
                    ),
                )
                backup_id = cur.fetchone()["id"]
            conn.commit()

        return jsonify(
            {
                "ok": True,
                "status": "success",
                "message": "Online Backup Completed Successfully",
                "backup_id": backup_id,
                "backup_time": ts,
                "backup_size": len(raw),
            }
        )
    except Exception as e:
        return jsonify({"ok": False, "status": "error", "message": f"Online backup failed: {e}"}), 500


@app.route("/api/backup/latest", methods=["POST"])
@app.route("/backup/latest", methods=["POST"])
def backup_latest():
    data = request.get_json(silent=True) or {}
    license_key = (data.get("license_key") or "").strip().upper()
    device_id = normalize_device_id(data)

    lic, err = _verify_active_license(license_key, device_id)
    if err:
        msg, code = err
        return jsonify({"ok": False, "status": "error", "message": msg}), code

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, backup_time, original_filename, backup_size, checksum_sha256
                FROM cloud_backups
                WHERE license_key=%s AND device_id=%s AND status='active'
                ORDER BY backup_time DESC, id DESC
                LIMIT 1
                """,
                (license_key, device_id),
            )
            row = cur.fetchone()

    if not row:
        return jsonify({"ok": False, "status": "empty", "message": "No online backup found"}), 404

    return jsonify({"ok": True, "status": "success", "backup": dict(row)})


@app.route("/api/backup/download", methods=["POST"])
@app.route("/backup/download", methods=["POST"])
def backup_download():
    data = request.get_json(silent=True) or {}
    license_key = (data.get("license_key") or "").strip().upper()
    device_id = normalize_device_id(data)
    backup_id = data.get("backup_id")

    lic, err = _verify_active_license(license_key, device_id)
    if err:
        msg, code = err
        return jsonify({"ok": False, "status": "error", "message": msg}), code

    with db() as conn:
        with conn.cursor() as cur:
            if backup_id:
                cur.execute(
                    """
                    SELECT * FROM cloud_backups
                    WHERE id=%s AND license_key=%s AND device_id=%s AND status='active'
                    """,
                    (int(backup_id), license_key, device_id),
                )
            else:
                cur.execute(
                    """
                    SELECT * FROM cloud_backups
                    WHERE license_key=%s AND device_id=%s AND status='active'
                    ORDER BY backup_time DESC, id DESC
                    LIMIT 1
                    """,
                    (license_key, device_id),
                )
            row = cur.fetchone()

    if not row:
        return jsonify({"ok": False, "status": "empty", "message": "No online backup found"}), 404

    try:
        bucket = _gcs_bucket()
        blob = bucket.blob(row["object_name"])
        encrypted = blob.download_as_bytes()
        raw = _backup_cipher().decrypt(encrypted)
        checksum = hashlib.sha256(raw).hexdigest()

        if row.get("checksum_sha256") and checksum != row["checksum_sha256"]:
            return jsonify({"ok": False, "status": "error", "message": "Backup verification failed"}), 500

        return jsonify(
            {
                "ok": True,
                "status": "success",
                "message": "Latest Online Backup Restored Successfully",
                "backup": {
                    "id": row["id"],
                    "backup_time": row["backup_time"],
                    "filename": row.get("original_filename") or "smartpay-backup.zip",
                    "backup_size": len(raw),
                    "backup_base64": base64.b64encode(raw).decode("utf-8"),
                },
            }
        )
    except Exception as e:
        return jsonify({"ok": False, "status": "error", "message": f"Online restore failed: {e}"}), 500


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
