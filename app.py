from flask import Flask, render_template, jsonify, request, Response
import requests
from datetime import datetime
import threading
import time
import sqlite3
import csv
import io
import os

app = Flask(__name__)

# -------- Adafruit IO Config --------
AIO_USERNAME = "yuvrajsingh01"
AIO_KEY = "aio_HrHe51WPGBO189y6s6J8zpk52XSG"
AIO_BASE_URL = f"https://io.adafruit.com/api/v2/{AIO_USERNAME}/feeds"

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
    """Create the cost_history table if it doesn't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cost_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                cost      REAL    NOT NULL,
                energy    REAL,
                recorded_at TEXT  NOT NULL
            )
        """)
        conn.commit()

init_db()

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

# -------- Cost Poller Background Thread --------
COST_POLL_INTERVAL = 60  # seconds
_last_saved_cost = None

def run_cost_poller():
    """Poll Adafruit IO for cost + energy and persist locally."""
    global _last_saved_cost
    while True:
        try:
            cost_data   = aio_get(FEEDS["cost"])
            energy_data = aio_get(FEEDS["energy"])
            cost_val    = cost_data.get("value")
            energy_val  = energy_data.get("value")
            if cost_val is not None:
                # Only save when the value actually changes
                if str(cost_val) != str(_last_saved_cost):
                    save_cost(cost_val, energy_val)
                    _last_saved_cost = cost_val
        except Exception as e:
            print("Cost poller error:", e)
        time.sleep(COST_POLL_INTERVAL)

# -------- Adafruit IO Helpers --------

def aio_get(feed_key):
    """Get the latest value from an Adafruit IO feed."""
    try:
        url = f"{AIO_BASE_URL}/{feed_key}/data/last"
        r = requests.get(url, headers=HEADERS, timeout=6)
        if r.status_code == 200:
            data = r.json()
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

cost_poller_thread = threading.Thread(target=run_cost_poller, daemon=True)
cost_poller_thread.start()


# -------- Routes --------

@app.route("/")
def index():
    return render_template("index.html")


# --- Dashboard data (all feeds at once) ---
@app.route("/api/dashboard")
def api_dashboard():
    result = {}
    sensor_feeds = ["voltage", "current", "power", "energy", "status", "alerts", "cost"]
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


# -------- Cost History Routes --------

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
