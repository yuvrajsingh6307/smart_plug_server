from flask import Flask, render_template, jsonify, request, Response
import requests
from datetime import datetime
import threading
import time
import sqlite3
import csv
import io
import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# -------- Adafruit IO Config --------
AIO_USERNAME = os.getenv("AIO_USERNAME")
AIO_KEY = os.getenv("AIO_KEY")
AIO_BASE_URL = f"https://io.adafruit.com/api/v2/{AIO_USERNAME}/feeds"

if not AIO_USERNAME or not AIO_KEY:
    print("CRITICAL ERROR: AIO_USERNAME or AIO_KEY not set in environment variables.")

HEADERS = {
    "X-AIO-Key": AIO_KEY,
    "Content-Type": "application/json"
}

FEEDS = {
    "relay":   "relay",
    "timer":   "timer",
    "reset":   "reset",
    "voltage": "voltage",
    "current": "current",
    "power":   "power",
    "energy":  "energy",
    "status":  "status",
    "alerts":  "alerts",
    "cost":    "cost",
}

# -------- In-memory scheduler store --------
schedules = []
schedule_lock = threading.Lock()

# -------- SQLite Cost History --------
DB_PATH = os.path.join(os.path.dirname(__file__), "cost_history.db")

def init_db():
    """Create the cost_history and settings tables."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cost_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                cost      REAL    NOT NULL,
                energy    REAL,
                recorded_at TEXT  NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mail_logs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                subject      TEXT NOT NULL,
                recipient    TEXT NOT NULL,
                status       TEXT NOT NULL,
                error_msg    TEXT,
                sent_at      TEXT NOT NULL
            )
        """)
        # Default settings
        defaults = [
            ("alert_email", ""),
            ("smtp_host", "smtp.gmail.com"),
            ("smtp_port", "465"),
            ("smtp_user", ""),
            ("smtp_pass", ""),
            ("power_threshold", "500"),
            ("cost_threshold", "100"),
            ("alerts_enabled", "0")
        ]
        for k, v in defaults:
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        conn.commit()

init_db()

def get_setting(key, default=""):
    with sqlite3.connect(DB_PATH) as conn:
        res = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return res[0] if res else default

def save_setting(key, value):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()

# -------- DB Helpers: Cost History --------

def save_cost(cost_val, energy_val=None):
    """Insert a cost reading into the local DB."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO cost_history (cost, energy, recorded_at) VALUES (?, ?, ?)",
                (float(cost_val), float(energy_val) if energy_val is not None else None,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()
    except Exception as e:
        print("DB save error:", e)

def get_cost_history(limit=100, offset=0):
    """Fetch cost history rows from the DB."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM cost_history ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
    return [dict(r) for r in rows]

def clear_cost_history():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM cost_history")
        conn.commit()

# -------- Email Helper --------
def send_email_alert(subject, body, force=False):
    """Sends an email alert. Use force=True to bypass the 'enabled' check (for testing)."""
    enabled = get_setting("alerts_enabled")
    recipient = get_setting("alert_email") or "triplem656@gmail.com"
    user = os.getenv("EMAIL")
    pw = os.getenv("PASSWORD")
    host = get_setting("smtp_host")
    port = int(get_setting("smtp_port"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # If not forced, check if alerts are globally enabled and credentials exist
    if not force:
        if enabled != "1" or not recipient or not user or not pw:
            return False
    else:
        # For tests, just ensure we have credentials
        if not user or not pw:
            print("ERROR: SMTP credentials missing in .env")
            return False

    status = "SUCCESS"
    err = None
    try:
        msg = MIMEMultipart()
        msg['From'] = user
        msg['To'] = recipient
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        # print(msg)
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context) as server:
            # print(server)
            server.login(user, pw)
            server.sendmail(user, recipient, msg.as_string())
            print('mail sent successfully')
    except Exception as e:
        print(e)
        status = "FAILED"
        err = str(e)
        print("Email error:", e)

    # Log to DB
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO mail_logs (subject, recipient, status, error_msg, sent_at) VALUES (?, ?, ?, ?, ?)",
                (subject, recipient, status, err, now)
            )
            conn.commit()
    except Exception as db_e:
        print("Mail log DB error:", db_e)

    return status == "SUCCESS"

# -------- Alert Monitor --------
_last_saved_cost = None
_last_power_alert_time = 0
_last_cost_alert_time = 0
ALERT_COOLDOWN = 3600 # 1 hour between emails per type

def run_alert_monitor():
    global _last_power_alert_time, _last_cost_alert_time, _last_saved_cost
    while True:
        try:
            # Check Power
            p_data = aio_get(FEEDS["power"])
            if p_data.get("value"):
                p_val = float(p_data["value"])
                p_thresh = float(get_setting("power_threshold", 500))
                if p_val > p_thresh and (time.time() - _last_power_alert_time) > ALERT_COOLDOWN:
                    send_email_alert("⚠️ High Power Usage Alert", 
                        f"Your smart plug detected high power usage: {p_val}W (Limit: {p_thresh}W)")
                    _last_power_alert_time = time.time()

            # Check Cost & Persist Cost
            c_data = aio_get(FEEDS["cost"])
            e_data = aio_get(FEEDS["energy"])
            if c_data.get("value"):
                c_val = float(c_data["value"])
                c_thresh = float(get_setting("cost_threshold", 100))
                
                # Persistent save
                if str(c_val) != str(_last_saved_cost):
                    save_cost(c_val, e_data.get("value"))
                    _last_saved_cost = c_val

                # Alert
                if c_val > c_thresh and (time.time() - _last_cost_alert_time) > ALERT_COOLDOWN:
                    send_email_alert("💰 High Energy Cost Alert", 
                        f"Your smart plug accumulated cost has exceeded the limit: ₹{c_val} (Limit: ₹{c_thresh})")
                    _last_cost_alert_time = time.time()

        except Exception as e:
            print("Monitor error:", e)
        time.sleep(60)

# -------- Adafruit IO Helpers --------

def aio_get(feed_key):
    """Get the latest value from an Adafruit IO feed."""
    try:
        print('getting values')
        url = f"{AIO_BASE_URL}/{feed_key}/data/last"
        r = requests.get(url, headers=HEADERS, timeout=6)
        # print(r.status_code)
        if r.status_code == 200:
            data = r.json()
            # print(data)
            return {
                "value": data.get("value"),
                "created_at": data.get("created_at"),
            }
        return {"value": None, "created_at": None, "error": r.text}
    except Exception as e:
        return {"value": None, "created_at": None, "error": str(e)}


def aio_publish(feed_key, value):
    """Publish a value to an Adafruit IO feed."""
    try:
        url = f"{AIO_BASE_URL}/{feed_key}/data"
        payload = {"value": str(value)}
        r = requests.post(url, headers=HEADERS, json=payload, timeout=6)
        if r.status_code in (200, 201):
            return {"success": True, "data": r.json()}
        return {"success": False, "error": r.text}
    except Exception as e:
        return {"success": False, "error": str(e)}


def aio_get_history(feed_key, limit=20):
    """Get recent feed data history."""
    try:
        url = f"{AIO_BASE_URL}/{feed_key}/data"
        params = {"limit": limit, "order": "desc"}
        r = requests.get(url, headers=HEADERS, params=params, timeout=8)
        if r.status_code == 200:
            return r.json()
        return []
    except Exception:
        return []


# -------- Scheduler Background Thread --------

def run_scheduler():
    while True:
        now = datetime.now()
        with schedule_lock:
            for sched in schedules:
                if sched.get("done"):
                    continue
                # Check if it's time to trigger
                trigger_time = sched.get("trigger_time")
                if trigger_time and now >= trigger_time:
                    action = sched.get("action", "ON")
                    val = "1" if action == "ON" else "0"
                    aio_publish(FEEDS["relay"], val)
                    sched["done"] = True
                    sched["triggered_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(5)


scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()

cost_poller_thread = threading.Thread(target=run_alert_monitor, daemon=True)
cost_poller_thread.start()


# -------- Routes --------

@app.route("/")
def index():
    return render_template("index.html")


# --- Dashboard data (all feeds at once) ---
@app.route("/api/dashboard")
def api_dashboard():
    result = {}
    # Fetching relay command feed + sensor feeds
    sensor_feeds = ["voltage", "current", "power", "energy", "status", "alerts", "cost", "relay"]
    for feed in sensor_feeds:
        result[feed] = aio_get(FEEDS[feed])
    return jsonify(result)


# --- Relay control ---
@app.route("/api/relay", methods=["POST"])
def api_relay():
    data = request.get_json()
    state = data.get("state")  # "ON" or "OFF"
    if state not in ("ON", "OFF"):
        return jsonify({"success": False, "error": "state must be ON or OFF"}), 400
    val = "1" if state == "ON" else "0"
    result = aio_publish(FEEDS["relay"], val)
    if not result.get("success"):
        print(f"[RELAY ERROR] state={state} val={val} error={result.get('error')}")
    return jsonify(result)


# --- Debug: list all feeds from Adafruit IO ---
@app.route("/api/debug/feeds")
def api_debug_feeds():
    """List every feed in your Adafruit IO account with its key."""
    try:
        url = f"https://io.adafruit.com/api/v2/{AIO_USERNAME}/feeds"
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code == 200:
            feeds = r.json()
            simplified = [{"name": f.get("name"), "key": f.get("key"), "last_value": f.get("last_value")} for f in feeds]
            return jsonify({"status": "ok", "feed_count": len(simplified), "feeds": simplified})
        return jsonify({"status": "error", "http_status": r.status_code, "body": r.text})
    except Exception as e:
        return jsonify({"status": "exception", "error": str(e)})


# --- Debug: test publish to relay feed ---
@app.route("/api/debug/relay-test", methods=["POST"])
def api_debug_relay_test():
    """Test publish to relay feed and return raw Adafruit IO response."""
    data = request.get_json() or {}
    val = data.get("value", "1")
    feed_key = data.get("feed_key", FEEDS["relay"])
    try:
        url = f"{AIO_BASE_URL}/{feed_key}/data"
        payload = {"value": str(val)}
        r = requests.post(url, headers=HEADERS, json=payload, timeout=8)
        return jsonify({
            "feed_key_used": feed_key,
            "value_sent": val,
            "http_status": r.status_code,
            "response": r.json() if r.headers.get("Content-Type", "").startswith("application/json") else r.text,
            "success": r.status_code in (200, 201)
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# --- Timer ---
@app.route("/api/timer", methods=["POST"])
def api_timer():
    data = request.get_json()
    seconds = data.get("seconds", 0)
    try:
        seconds = int(seconds)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "seconds must be an integer"}), 400
    result = aio_publish(FEEDS["timer"], seconds)
    return jsonify(result)


# --- Safety Reset ---
@app.route("/api/reset", methods=["POST"])
def api_reset():
    result = aio_publish(FEEDS["reset"], "1")
    return jsonify(result)


# --- History ---
@app.route("/api/history/<feed_key>")
def api_history(feed_key):
    if feed_key not in FEEDS:
        return jsonify({"error": "Unknown feed"}), 400
    limit = request.args.get("limit", 20, type=int)
    data = aio_get_history(FEEDS[feed_key], limit=limit)
    return jsonify(data)


# --- Schedule list ---
@app.route("/api/schedules", methods=["GET"])
def api_schedules_get():
    with schedule_lock:
        out = []
        for s in schedules:
            out.append({
                "id": s["id"],
                "action": s["action"],
                "trigger_time": s["trigger_time"].strftime("%Y-%m-%d %H:%M:%S") if s.get("trigger_time") else None,
                "label": s.get("label", ""),
                "done": s.get("done", False),
                "triggered_at": s.get("triggered_at"),
            })
    return jsonify(out)


# --- Add Schedule ---
@app.route("/api/schedules", methods=["POST"])
def api_schedules_post():
    data = request.get_json()
    action = data.get("action", "ON")
    label = data.get("label", "")
    trigger_str = data.get("trigger_time")  # "YYYY-MM-DD HH:MM"

    if action not in ("ON", "OFF"):
        return jsonify({"success": False, "error": "action must be ON or OFF"}), 400
    try:
        trigger_time = datetime.strptime(trigger_str, "%Y-%m-%dT%H:%M")
    except Exception:
        return jsonify({"success": False, "error": "trigger_time format: YYYY-MM-DDTHH:MM"}), 400

    with schedule_lock:
        new_id = len(schedules) + 1
        schedules.append({
            "id": new_id,
            "action": action,
            "trigger_time": trigger_time,
            "label": label,
            "done": False,
            "triggered_at": None,
        })

    return jsonify({"success": True, "id": new_id})


# --- Delete Schedule ---
@app.route("/api/schedules/<int:sched_id>", methods=["DELETE"])
def api_schedules_delete(sched_id):
    with schedule_lock:
        global schedules
        schedules = [s for s in schedules if s["id"] != sched_id]
    return jsonify({"success": True})


# --- Clear done schedules ---
@app.route("/api/schedules/clear-done", methods=["POST"])
def api_schedules_clear_done():
    with schedule_lock:
        global schedules
        schedules = [s for s in schedules if not s.get("done")]
    return jsonify({"success": True})


# -------- Alert Settings Routes --------

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    keys = ["alert_email", "smtp_host", "smtp_port", "smtp_user", "power_threshold", "cost_threshold", "alerts_enabled"]
    return jsonify({k: get_setting(k) for k in keys})


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.get_json()
    for k, v in data.items():
        # Don't overwrite password if it's empty in the request (unless user wants to clear it)
        if k == "smtp_pass" and not v:
            continue
        save_setting(k, v)
    return jsonify({"success": True})


@app.route("/api/test-email", methods=["POST"])
def api_test_email():
    data = request.get_json() or {}
    subject = data.get("subject", "🔌 Smart Plug Test")
    body = data.get("body", "This is a test email from your Smart Plug Dashboard.")
    # Use force=True so tests work even if alerts are disabled
    success = send_email_alert(subject, body, force=True)
    return jsonify({"success": success})


@app.route("/api/mail-logs")
def api_mail_logs():
    """Return recent mail sending logs."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM mail_logs ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/mail-logs", methods=["DELETE"])
def api_clear_mail_logs():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM mail_logs")
        conn.commit()
    return jsonify({"success": True})

@app.route("/api/cost-history")
def api_cost_history():
    """Return paginated local cost history."""
    limit  = request.args.get("limit",  100, type=int)
    offset = request.args.get("offset",   0, type=int)
    rows = get_cost_history(limit=limit, offset=offset)
    return jsonify(rows)


@app.route("/api/cost-history/export")
def api_cost_history_export():
    """Download cost history as a CSV file."""
    rows = get_cost_history(limit=10000)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["id", "cost", "energy", "recorded_at"])
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=cost_history.csv"}
    )


@app.route("/api/cost-history", methods=["DELETE"])
def api_cost_history_clear():
    """Delete all cost history records."""
    clear_cost_history()
    return jsonify({"success": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
