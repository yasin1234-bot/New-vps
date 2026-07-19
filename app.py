import os
import json
import signal
import requests
import shutil
import zipfile
import hashlib
import subprocess
import psutil
import threading
import io
import time
import asyncio
import multiprocessing
import logging
import re
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, abort, render_template_string
from flask_sqlalchemy import SQLAlchemy 
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash
from pathlib import Path
from functools import wraps

# টেলিগ্রাম লাইব্রেরি ইমপোর্ট
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

app = Flask(__name__)
app.secret_key = "yasin-vps-secret-2025" 

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# গ্লোবাল ডিকশনারি (সার্ভার ভিত্তিক লগের জন্য - যাতে মিক্সআপ না হয়)
SERVER_LOGS = {}

def get_and_update_count():
    file_path = "counter.txt"
    if not os.path.exists(file_path):
        with open(file_path, "w") as f:
            f.write("0")
    with open(file_path, "r") as f:
        data = f.read().strip()
        count = int(data) if data else 0

    session.permanent = True
    if not session.get('has_visited'):
        count += 1
        with open(file_path, "w") as f:
            f.write(str(count))
        session['has_visited'] = True
    return count
    
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app) 

class Song(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"
SERVERS_DIR = BASE_DIR / "servers"
UPLOAD_FOLDER = 'static/uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
SERVERS_DIR.mkdir(exist_ok=True)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "TAHMID CODEX@@@1")

RUNNING_PROCESSES = {}
RESET_TIMERS = {}

THEME_PRESETS = {
    "purple": "#a855f7",
    "green":  "#00ff41",
    "blue":   "#38bdf8",
    "red":    "#ef4444",
    "amber":  "#fbbf24",
    "cyan":   "#06b6d4",
    "pink":   "#ec4899",
    "lime":   "#84cc16",
}

# ─── Data helpers ─────────────────────────────────────────────────────────────

def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {
        "servers": {},
        "users": {},
        "settings": {
            "maintenance": False,
            "maintenance_msg": "System under maintenance.",
            "theme_color": "#a855f7"
        }
    }

def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def get_theme_color():
    data = load_data()
    return data.get("settings", {}).get("theme_color", "#a855f7")

@app.context_processor
def inject_theme():
    return {"theme_color": get_theme_color()}

# ─── Process helpers ──────────────────────────────────────────────────────────

def is_process_alive(pid):
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

def kill_process(pid):
    try:
        p = psutil.Process(pid)
        children = p.children(recursive=True)
        p.terminate()
        for child in children:
            try: child.terminate()
            except Exception: pass
        try: p.wait(timeout=5)
        except psutil.TimeoutExpired: p.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

def get_run_command(main_file):
    return ["python", "-u", main_file]

# ─── Expiry Checking Mechanism ────────────────────────────────────────────────

def check_server_expiry(name, data=None):
    if not data:
        data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return False
        
    expired_at_str = cfg.get("expired_at")
    if expired_at_str:
        try:
            expired_at = datetime.fromisoformat(expired_at_str)
            if datetime.now() > expired_at:
                pid = cfg.get("pid")
                if pid:
                    kill_process(pid)
                if name in RUNNING_PROCESSES:
                    try: RUNNING_PROCESSES[name]["proc"].terminate()
                    except Exception: pass
                    del RUNNING_PROCESSES[name]
                
                if cfg["status"] != "expired":
                    cfg["status"] = "expired"
                    cfg["pid"] = None
                    data["servers"][name] = cfg
                    save_data(data)
                return True
        except Exception:
            pass
    return False

def server_access_guard(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        name = kwargs.get("name") or request.view_args.get("name")
        if name:
            data = load_data()
            if check_server_expiry(name, data):
                return jsonify({
                    "success": False, 
                    "error": "Your server license has expired! Please renew or upgrade your plan with coins to access."
                }), 403
        return f(*args, **kwargs)
    return decorated

def _sync_process_status():
    data = load_data()
    changed = False
    for name, cfg in list(data["servers"].items()):
        expired_at_str = cfg.get("expired_at")
        if expired_at_str:
            try:
                exp = datetime.fromisoformat(expired_at_str)
                if datetime.now() > exp:
                    pid = cfg.get("pid")
                    if pid: kill_process(pid)
                    cfg["status"] = "expired"
                    cfg["pid"] = None
                    changed = True
                    continue
            except Exception: pass

        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            changed = True
    if changed:
        save_data(data)

_sync_process_status()

# ─── Decorators ───────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        data = load_data()
        settings = data.get("settings", {})
        if settings.get("maintenance") and session.get("username") != "__admin__":
            return render_template("maintenance.html", message=settings.get("maintenance_msg", "Under maintenance"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

# ─── Auto-reset helpers ────────────────────────────────────────────────────────

def _auto_reset_seconds(cfg):
    ar = cfg.get("auto_reset", {})
    y = ar.get("years", 0) or 0
    d = ar.get("days", 0) or 0
    h = ar.get("hours", 0) or 0
    m = ar.get("minutes", 0) or 0
    s = ar.get("seconds", 0) or 0
    return int(y * 365 * 24 * 3600 + d * 24 * 3600 + h * 3600 + m * 60 + s)

def _do_auto_reset(name):
    try:
        data = load_data()
        if check_server_expiry(name, data):
            return
            
        cfg = data["servers"].get(name)
        if not cfg: return
        pid = cfg.get("pid")

        if name in RUNNING_PROCESSES:
            entry = RUNNING_PROCESSES[name]
            proc = entry["proc"]
            try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try: proc.terminate()
                except Exception: pass
            try: proc.wait(timeout=5)
            except Exception:
                try: proc.kill()
                except Exception: pass
            try: entry["log_file"].close()
            except Exception: pass
            del RUNNING_PROCESSES[name]
        elif pid:
            kill_process(pid)

        log_path = SERVERS_DIR / name / "logs.txt"
        try:
            with open(log_path, "w") as lf:
                lf.write(f"\n{'='*50}\n[{datetime.now().isoformat()}] AUTO RESET triggered\n{'='*50}\n")
        except Exception: pass

        main_file = cfg.get("main_file") or "main.py"
        if not main_file.endswith(".py"):
            return

        extract_dir = SERVERS_DIR / name / "extracted"
        main_path = extract_dir / main_file

        if main_path.exists():
            cmd = get_run_command(main_file)
            env = os.environ.copy()
            env["PORT"] = str(cfg.get("port", 8080))
            log_file = open(log_path, "a")
            
            kwargs = {}
            if os.name != 'nt':
                kwargs['preexec_fn'] = os.setsid
            else:
                kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

            proc = subprocess.Popen(
                cmd, cwd=str(extract_dir), stdout=log_file, stderr=log_file, env=env, shell=False, **kwargs
            )
            RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
            cfg["status"] = "running"
            cfg["pid"] = proc.pid
        else:
            cfg["status"] = "stopped"
            cfg["pid"] = None

        data["servers"][name] = cfg
        save_data(data)

        total = _auto_reset_seconds(cfg)
        if cfg.get("auto_reset", {}).get("enabled") and total > 0:
            _schedule_reset(name, total)
    except Exception as e:
        print("Auto reset error:", e)

def _schedule_reset(name, total_seconds):
    if name in RESET_TIMERS:
        try: RESET_TIMERS[name]["timer"].cancel()
        except Exception: pass

    t = threading.Timer(total_seconds, _do_auto_reset, args=[name])
    t.daemon = True
    t.start()
    RESET_TIMERS[name] = {
        "timer": t,
        "started_at": datetime.now().isoformat(),
        "total_seconds": total_seconds
    }

def _init_reset_timers():
    data = load_data()
    for name, cfg in data["servers"].items():
        ar = cfg.get("auto_reset", {})
        if ar.get("enabled"):
            total = _auto_reset_seconds(cfg)
            if total > 0:
                _schedule_reset(name, total)

_init_reset_timers()

# ─── REAL COINS API ───────────────────────────────────────────────────────────

@app.route("/api/claim_coins", methods=["POST"])
@login_required
def claim_coins_api():
    username = session.get("username")
    data = load_data()
    
    if username not in data["users"]:
        return jsonify({"status": "error", "msg": "User not found."}), 404
        
    user = data["users"][username]
    
    if "coins" not in user:
        user["coins"] = 0
    if "last_coin_claim" not in user:
        user["last_coin_claim"] = None

    payload = request.get_json() or {}
    today_str = payload.get("local_date")
    
    if not today_str:
        today_str = datetime.now().strftime("%Y-%m-%d")
    
    if user["last_coin_claim"] == today_str:
        return jsonify({"status": "error", "msg": "You have already claimed your 20 coins today! Check back after midnight."}), 200
        
    user["coins"] += 20
    user["last_coin_claim"] = today_str
    
    data["users"][username] = user
    data["users"][username]["free_server_claimed"] = True
    save_data(data)
    
    return jsonify({
        "status": "success", 
        "msg": f"Successfully added 20 coins to your account! Total Balance: {user['coins']} Coins."
    }), 200

# ─── UI & User API Endpoints For Dashboard Compatibility ──────────────────────

@app.route("/api/user_info")
@login_required
def get_user_info():
    username = session.get("username")
    data = load_data()
    user = data["users"].get(username, {})
    return jsonify({
        "name": username,
        "email": user.get("email", f"{username}@gmail.com"),
        "coins": user.get("coins", 0),
        "free_server_claimed": user.get("free_server_claimed", False)
    })

@app.route("/api/user/update_all", methods=["POST"])
@login_required
def update_user_profile():
    username = session.get("username")
    payload = request.get_json()
    action = payload.get("action")
    data = load_data()
    
    if action == "profile":
        new_name = payload.get("name", "").strip()
        if not new_name:
            return jsonify({"success": False, "error": "Invalid name"}), 400
        if new_name != username and new_name in data["users"]:
            return jsonify({"success": False, "error": "Username already exists"}), 400
        
        data["users"][new_name] = data["users"].pop(username)
        for sname, scfg in list(data["servers"].items()):
            if scfg.get("owner") == username:
                data["servers"][sname]["owner"] = new_name
        data["servers"][sname]["owner"] = new_name
        save_data(data)
        session["username"] = new_name
        return jsonify({"success": True})
        
    elif action == "password":
        current_pw = payload.get("current_password", "").strip()
        new_pw = payload.get("new_password", "").strip()
        user_info = data["users"].get(username, {})
        if user_info.get("password_hash") != hash_password(current_pw):
            return jsonify({"success": False, "error": "Incorrect password"}), 400
        data["users"][username]["password_hash"] = hash_password(new_pw)
        data["users"][username]["raw_password"] = new_pw
        save_data(data)
        return jsonify({"success": True})
        
    return jsonify({"success": False, "error": "Invalid action"}), 400

@app.route("/api/delete_account", methods=["POST"])
@login_required
def delete_own_account():
    username = session.get("username")
    data = load_data()
    to_delete = [k for k, v in data["servers"].items() if v.get("owner") == username]
    for name in to_delete:
        pid = data["servers"][name].get("pid")
        if pid: kill_process(pid)
        if name in RUNNING_PROCESSES:
            try: RUNNING_PROCESSES[name]["proc"].terminate()
            except Exception: pass
            del RUNNING_PROCESSES[name]
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
        del data["servers"][name]
    data["users"].pop(username, None)
    save_data(data)
    session.clear()
    return jsonify({"success": True})

# ─── CLAIM FREE SERVER ────────────────────────────────────────────────────────

@app.route("/add", methods=["POST"])
@login_required
def claim_free_server():
    data = load_data()
    username = session["username"]
    
    if username not in data["users"]:
        return jsonify({"success": False, "error": "User session mismatch. Please login again."}), 400
        
    user_info = data["users"][username]
    user_email = user_info.get("email", f"{username}@gmail.com")
    
    if user_info.get("free_server_claimed") is True:
        return jsonify({"success": False, "error": "You have already claimed your free server! Multiple free servers are not allowed per account."}), 400

    if request.is_json:
        payload = request.get_json() or {}
    else:
        payload = request.form

    server_name = payload.get("name", "").strip().replace(" ", "-")
    runtime = "python"

    if not server_name:
        return jsonify({"success": False, "error": "Invalid server name"}), 400

    if server_name in data["servers"]:
        return jsonify({"success": False, "error": "Server project name already exists. Try again."}), 400
        
    created_time = datetime.now()
    expire_time = created_time + timedelta(days=5)

    cfg = {
        "name": server_name,
        "owner": username,
        "owner_email": user_email,
        "runtime": runtime,  
        "status": "stopped",
        "main_file": "main.py",
        "port": 8080,
        "packages": [],
        "pid": None,
        "created": created_time.isoformat(),
        "expired_at": expire_time.isoformat(), 
        "ram_limit": "256MB",
        "cpu_limit": "25%",
        "storage_limit": "1GB",
        "plan_type": "free",
        "plan_name": "Python",
        "duration_type": "5days",
        "auto_reset": {"enabled": False, "years": 0, "days": 0, "hours": 0, "minutes": 0, "seconds": 0}
    }
    
    data["users"][username]["free_server_claimed"] = True
    data["servers"][server_name] = cfg
    
    save_data(data)
    (SERVERS_DIR / server_name / "extracted").mkdir(parents=True, exist_ok=True)
    
    return jsonify({"success": True})

# ─── PREMIUM PLAN PURCHASE & RENEW API ────────────────────────────────────────

@app.route('/api/buy_plan', methods=['POST'])
@login_required
def buy_plan():
    username = session.get('username')
    data = request.json or {}
    ram_limit = data.get('ram_limit', '256MB')
    duration = data.get('duration', '1month') 
    existing_server_name = data.get('server_name') 

    pricing = {
        '256MB': {'1month': 55, '1year': 330},
        '512MB': {'1month': 77, '1year': 605},
        '1GB':   {'1month': 110, '1year': 1100},
        '2GB':   {'1month': 220, '1year': 2200}
    }
    
    specs = {
        '256MB': {'cpu': '25%', 'storage': '2GB'},
        '512MB': {'cpu': '50%', 'storage': '4GB'},
        '1GB':   {'cpu': '100%', 'storage': '8GB'},
        '2GB':   {'cpu': '200%', 'storage': '12GB'}
    }

    if ram_limit not in pricing or duration not in pricing[ram_limit]:
        return jsonify({'status': 'error', 'msg': 'অবৈধ প্ল্যান ডেটা সিলেক্ট করা হয়েছে!'}), 400

    cost = pricing[ram_limit][duration]
    
    full_data = load_data()
    user_info = full_data["users"].get(username)
    if not user_info:
        return jsonify({'status': 'error', 'msg': 'ইউজার পাওয়া যায়নি!'}), 404
        
    current_coins = user_info.get("coins", 0)
    user_email = user_info.get("email", f"{username}@gmail.com")

    if current_coins < cost:
        return jsonify({'status': 'error', 'msg': f'আপনার পর্যাপ্ত কয়েন নেই! এই প্যাকেজের জন্য {cost} কয়েন প্রয়োজন।'}), 400

    days_to_add = 365 if duration == '1year' else 30
    determined_runtime = "python"

    if existing_server_name and existing_server_name in full_data["servers"]:
        cfg = full_data["servers"][existing_server_name]
        if cfg.get("owner") != username:
            return jsonify({'status': 'error', 'msg': 'অননুমোদিত রিকোয়েস্ট!'}), 403
        if cfg.get("plan_type") == "free":
            return jsonify({'status': 'error', 'msg': 'দ্বঃখিত, ফ্রি ট্রয়ার সার্ভারের মেয়াদ শেষ হওয়ার পর তা আর আপডেট করা যাবে না! এটি লক হয়ে গেছে।'}), 400
        
        cfg["expired_at"] = (datetime.now() + timedelta(days=days_to_add)).isoformat()
        cfg["status"] = "stopped"
        cfg["ram_limit"] = ram_limit
        cfg["cpu_limit"] = specs[ram_limit]['cpu']
        cfg["storage_limit"] = specs[ram_limit]['storage']
        cfg["duration_type"] = duration
        cfg["plan_type"] = "premium"
        cfg["plan_name"] = "Python"
        cfg["runtime"] = determined_runtime  
        cfg["main_file"] = "main.py"
        
        full_data["users"][username]["coins"] = current_coins - cost
        full_data["servers"][existing_server_name] = cfg
        save_data(full_data)
        
        return jsonify({
            'status': 'success', 
            'msg': 'আপনার প্রিমিয়াম সার্ভারটি সফলভাবে রিনিউ/আপডেট করা হয়েছে!', 
            'remaining_coins': full_data["users"][username]["coins"]
        }), 200

    full_data["users"][username]["coins"] = current_coins - cost
    created_time = datetime.now()
    exp_time = (created_time + timedelta(days=days_to_add)).isoformat()
    
    server_unique_suffix = int(time.time())
    server_name = f"premium-{ram_limit.lower()}-{username.lower()}-{server_unique_suffix}"
    
    cfg = {
        "name": server_name,
        "owner": username,  
        "owner_email": user_email,
        "runtime": determined_runtime,  
        "status": "stopped",
        "main_file": "main.py",
        "port": 8080,
        "packages": [],
        "pid": None,
        "created": created_time.isoformat(),
        "expired_at": exp_time,
        "ram_limit": ram_limit,
        "cpu_limit": specs[ram_limit]['cpu'],
        "storage_limit": specs[ram_limit]['storage'],
        "plan_type": "premium",  
        "plan_name": "Python", 
        "duration_type": duration,
        "auto_reset": {"enabled": False, "years": 0, "days": 0, "hours": 0, "minutes": 0, "seconds": 0}
    }

    full_data["servers"][server_name] = cfg
    save_data(full_data)
    
    (SERVERS_DIR / server_name / "extracted").mkdir(parents=True, exist_ok=True)
    time.sleep(0.1)
    
    return jsonify({
        'status': 'success', 
        'msg': 'প্রিমিয়াম সাবস্ক্রিপশন সফলভাবে কেনা হয়েছে!', 
        'remaining_coins': full_data["users"][username]["coins"]
    }), 200

# ─── Auth routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if session.get("username"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        if request.is_json:
            payload = request.get_json() or {}
            username = payload.get('username', '').strip()
            email = payload.get('email', '').strip()
            pwd = payload.get('password')
            cpwd = payload.get('confirm_password')
        else:
            username = request.form.get('username', '').strip()
            email = request.form.get('email', '').strip()
            pwd = request.form.get('password')
            cpwd = request.form.get('confirm_password')

        if not username or not email or not pwd or not cpwd:
            return jsonify({'status': 'error', 'msg': 'সবগুলো ঘর পূরণ করা বাধ্যতামূলক!'}), 400

        if len(pwd) < 6:
            return jsonify({'status': 'error', 'msg': 'পাসওয়ার্ডটি ছোট! কমপক্ষে ৬ অক্ষরের পাসওয়ার্ড দিন।'}), 400

        if pwd != cpwd:
            return jsonify({'status': 'error', 'msg': 'পাসওয়ার্ড দুটি মেলেনি! কনফার্ম পাসওয়ার্ডটি আবার চেক করুন।'}), 400

        data = load_data()
        
        if username in data["users"]:
            return jsonify({'status': 'error', 'msg': 'এই ইউজারনেমটি ইতিমধ্যে ব্যবহৃত হয়েছে! অন্য একটি দিন।'}), 400
            
        for u_info in data["users"].values():
            if u_info.get("email") == email:
                return jsonify({'status': 'error', 'msg': 'এই ইমেইলটি দিয়ে ইতিমধ্যে অ্যাকাউন্ট খোলা হয়েছে!'}), 400

        data["users"][username] = {
            "fname": username,
            "lname": "",
            "joined": datetime.now().isoformat(),
            "password_hash": hash_password(pwd), 
            "raw_password": pwd,
            "email": email,
            "pfp": 'default.png',
            "server_limit": 10,
            "role": "free",
            "status": "active",
            "coins": 0,
            "last_coin_claim": None,
            "free_server_claimed": False
        }
        save_data(data)
        
        return jsonify({'status': 'success', 'url': url_for('dashboard')})
        
    return render_template('signup.html')

@app.route("/login", methods=["GET", "POST"])
def login():
    current_visit = get_and_update_count() 
    data_settings = load_data() 
    logo_url = data_settings.get('settings', {}).get('logo_url', 'https://i.postimg.cc/default.png')
    theme_color = data_settings.get('settings', {}).get('theme_color', '#6366f1')

    if request.method == "POST":
        input_email = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        
        if not input_email or not password:
            return jsonify({"errorType": "both"}), 400
            
        data = load_data()
        user_found = None
        username_found = None
        
        for u_name, u_info in data["users"].items():
            if u_info.get("email") == input_email:
                user_found = u_info
                username_found = u_name
                break
        
        if user_found:
            stored_hash = user_found.get("password_hash", "")
            if stored_hash and stored_hash != hash_password(password):
                return jsonify({"errorType": "password"}), 400
            
            session["username"] = username_found
            return jsonify({"url": url_for("dashboard")}), 200
        else:
            return jsonify({"errorType": "username"}), 400

    return render_template("login.html", error=None, logo_url=logo_url, theme_color=theme_color, visit=current_visit)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ─── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/dashboard")
@login_required
def dashboard():
    try:
        username = session["username"]
        data = load_data()
        
        settings_file = 'settings.json'
        if os.path.exists(settings_file):
            try:
                with open(settings_file, 'r', encoding='utf-8') as f:
                    dashboard_settings = json.load(f)
            except:
                dashboard_settings = {"music_volume": 10, "music_url": ""}
        else:
            dashboard_settings = {"music_volume": 10, "music_url": ""}

        user_servers = {k: v for k, v in data["servers"].items() if str(v.get("owner", "")).strip().lower() == username.lower()}
        
        changed = False
        for name in list(user_servers.keys()):
            if check_server_expiry(name, data):
                changed = True
                continue
                
            cfg = data["servers"][name]
            pid = cfg.get("pid")
            if pid and not is_process_alive(pid):
                cfg["status"] = "stopped"
                cfg["pid"] = None
                data["servers"][name] = cfg
                changed = True
                
        if changed:
            save_data(data)
            user_servers = {k: v for k, v in data["servers"].items() if str(v.get("owner", "")).strip().lower() == username.lower()}
            
        running = sum(1 for v in user_servers.values() if v.get("status") == "running")
        
        free_servers_count = 0
        premium_servers_count = 0
        
        for k, v in user_servers.items():
            plan_type_lower = str(v.get("plan_type", "")).lower()
            if plan_type_lower == "premium":
                premium_servers_count += 1
            else:
                free_servers_count += 1
        
        return render_template("dashboard.html", 
                               servers=user_servers, 
                               running=running, 
                               total=len(user_servers), 
                               free_count=free_servers_count,       
                               premium_count=premium_servers_count, 
                               username=username, 
                               settings=dashboard_settings)
    except Exception as e:
        return f"System Error: {str(e)}", 500

@app.route("/admin/music/volume", methods=["POST"])
def update_settings():
    try:
        new_volume = request.form.get("volume")
        settings_file = 'settings.json'
        settings = {"music_volume": 10, "music_url": ""}
        
        if os.path.exists(settings_file):
            with open(settings_file, 'r', encoding='utf-8') as f:
                try: settings = json.load(f)
                except: pass

        if new_volume is not None:
            settings['music_volume'] = int(new_volume)
        
        with open(settings_file, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=4)
        return "Success"
    except Exception as e:
        return f"Error: {str(e)}", 500

# ─── System stats API ─────────────────────────────────────────────────────────

@app.route("/api/stats")
@login_required
def system_stats():
    cpu = psutil.cpu_percent(interval=0.2)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    return jsonify({"cpu": cpu, "ram": ram, "disk": disk})

# ─── Server Live Usage API (For server.html updates) ──────────────────────────

@app.route("/server/<name>/usage")
@login_required
def server_live_usage(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404

    pid = cfg.get("pid")
    cpu_usage = 0.0
    ram_usage = 0.0
    storage_usage = 0.0

    if pid and is_process_alive(pid):
        try:
            p = psutil.Process(pid)
            cpu_usage = p.cpu_percent(interval=None)
            ram_usage = p.memory_info().rss / (1024 * 1024) 
        except Exception:
            pass

    extract_dir = SERVERS_DIR / name / "extracted"
    if extract_dir.exists():
        total_size = 0
        for dirpath, dirnames, filenames in os.walk(extract_dir):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp):
                    total_size += os.path.getsize(fp)
        storage_usage = total_size / (1024 * 1024) 

    return jsonify({
        "success": True,
        "status": cfg.get("status", "stopped"),
        "live_cpu": f"{cpu_usage:.1f}%",
        "live_ram": f"{ram_usage:.1f} MB",
        "live_storage": f"{storage_usage:.2f} MB",
        "limits": {
            "cpu": cfg.get("cpu_limit", "25%"),
            "ram": cfg.get("ram_limit", "256MB"),
            "storage": cfg.get("storage_limit", "1GB")
        }
    })

# ─── Server management ────────────────────────────────────────────────────────

@app.route("/server/create", methods=["POST"])
@login_required
def create_server():
    name = request.form.get("name", "").strip().replace(" ", "-")
    if not name:
        return redirect(url_for("dashboard"))
    data = load_data()
    if name in data["servers"]:
        return redirect(url_for("dashboard"))
    
    runtime = "python"
        
    cfg = {
        "name": name,
        "owner": session["username"],
        "runtime": runtime,
        "status": "stopped",
        "main_file": "main.py",
        "port": 8080,
        "packages": [],
        "pid": None,
        "created": datetime.now().isoformat(),
        "expired_at": (datetime.now() + timedelta(days=30)).isoformat(),
        "ram_limit": "256MB",
        "cpu_limit": "25%",
        "storage_limit": "2GB",
        "plan_type": "free",
        "plan_name": "Python",
        "duration_type": "30days",
        "auto_reset": {"enabled": False, "years": 0, "days": 0, "hours": 0, "minutes": 0, "seconds": 0}
    }
    data["servers"][name] = cfg
    save_data(data)
    (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
    return redirect(url_for("server_detail", name=name))

@app.route("/server/delete/<name>", methods=["POST"])
@login_required
def delete_server(name):
    data = load_data()
    cfg = data.get("servers", {}).get(name) 
    
    if cfg and (cfg.get("owner") == session["username"] or session.get("admin")):
        pid = cfg.get("pid")
        if pid: kill_process(pid)
        if name in RUNNING_PROCESSES:
            try: RUNNING_PROCESSES[name]["proc"].terminate()
            except Exception: pass
            del RUNNING_PROCESSES[name]
        if name in RESET_TIMERS:
            try: RESET_TIMERS[name]["timer"].cancel()
            except Exception: pass
            del RESET_TIMERS[name]
        if name in data["servers"]:
            del data["servers"][name]
        save_data(data)
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
    return redirect(url_for("dashboard")) 

@app.route("/server/<name>")
@login_required
def server_detail(name):
    try:
        data = load_data()
        
        if check_server_expiry(name, data):
            cfg = data["servers"].get(name, {})
            return render_template("expired.html", server_name=name, plan_type=cfg.get("plan_type", "free"))
            
        cfg = data["servers"].get(name)
        if not cfg: return "Server not found", 404
        
        current_user = session.get("username")
        is_admin = session.get("admin") == True 

        if not is_admin:
            if cfg.get("owner") != current_user: return "Unauthorized", 403
        
        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            data["servers"][name] = cfg
            save_data(data)

        if "auto_reset" not in cfg:
            cfg["auto_reset"] = {"enabled": False, "years": 0, "days": 0, "hours": 0, "minutes": 0, "seconds": 0}

        settings_file = 'settings.json'
        server_settings = json.load(open(settings_file)) if os.path.exists(settings_file) else {}
        extract_dir = SERVERS_DIR / name / "extracted"
        files = list_files(extract_dir)
        
        cpu_usage = 0.0
        ram_usage = 0.0
        if pid and is_process_alive(pid):
            try:
                p = psutil.Process(pid)
                cpu_usage = p.cpu_percent(interval=None)
                ram_usage = p.memory_info().rss / (1024 * 1024)
            except Exception: pass

        total_size = 0
        if extract_dir.exists():
            for dirpath, _, filenames in os.walk(extract_dir):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if not os.path.islink(fp): total_size += os.path.getsize(fp)
        storage_usage = total_size / (1024 * 1024)

        live_stats = {
            "cpu": f"{cpu_usage:.1f}%",
            "ram": f"{ram_usage:.1f} MB",
            "storage": f"{storage_usage:.2f} MB"
        }
        
        return render_template("server.html", server_name=name, config=cfg, files=files, 
                               theme_color=cfg.get('theme_color', '#a855f7'), 
                               settings=server_settings, live_stats=live_stats)
            
    except Exception as e:
        return str(e), 500

def list_files(directory, base=""):
    result = []
    if not directory.exists(): return result
    try:
        for entry in sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name)):
            rel = f"{base}/{entry.name}" if base else entry.name
            if entry.is_dir():
                result.append({"name": entry.name, "path": rel, "type": "dir", "size": 0})
                result.extend(list_files(entry, rel))
            else:
                result.append({"name": entry.name, "path": rel, "type": "file", "size": entry.stat().st_size})
    except Exception: pass
    return result

# ─── Upload ───

@app.route("/server/<name>/upload", methods=["POST"])
@login_required
@server_access_guard
def upload_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg: return jsonify({"success": False, "error": "Not found"}), 404
    if "file" not in request.files: return jsonify({"success": False, "error": "No file"})
    f = request.files["file"]
    extract_dir = SERVERS_DIR / name / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    upload_path = SERVERS_DIR / name / f"upload_{f.filename}"
    f.save(upload_path)
    
    if f.filename.endswith(".zip"):
        try:
            with zipfile.ZipFile(upload_path, "r") as z:
                z.extractall(extract_dir)
            upload_path.unlink(missing_ok=True)
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})
    else:
        dest = extract_dir / f.filename
        shutil.copy(upload_path, dest)
        upload_path.unlink(missing_ok=True)
        
    all_files = os.listdir(extract_dir)
    
    if "main.py" in all_files:
        cfg["main_file"] = "main.py"
    elif "bot.py" in all_files:
        cfg["main_file"] = "bot.py"
    elif "app.py" in all_files:
        cfg["main_file"] = "app.py"
    else:
        py_files = [f for f in all_files if f.endswith(".py")]
        cfg["main_file"] = py_files[0] if py_files else "main.py"
            
    data["servers"][name] = cfg
    save_data(data)
    
    updated_files = list_files(extract_dir)
    return jsonify({"success": True, "files": [f["name"] for f in updated_files]})

@app.route('/server/<server_name>/files/delete_all', methods=['POST'])
@login_required
def delete_all_files(server_name):
    if check_server_expiry(server_name):
        return jsonify({"success": False, "error": "Expired server"}), 403
        
    folder_path = SERVERS_DIR / server_name / "extracted"
    try:
        if folder_path.exists():
            for item in os.listdir(folder_path):
                item_path = folder_path / item
                if item_path.is_file() or item_path.is_symlink(): 
                    os.unlink(item_path)
                elif item_path.is_dir(): 
                    shutil.rmtree(item_path)
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Folder not found"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/server/<server_name>/files/delete', methods=['POST'])
@login_required  
def delete_file(server_name):
    if check_server_expiry(server_name):
        return jsonify({"success": False, "error": "Expired server"}), 403
        
    data = request.get_json()
    file_name = data.get('name')
    
    if not file_name:
        return jsonify({'success': False, 'error': 'No file name provided'}), 400
        
    base_dir = (SERVERS_DIR / server_name / "extracted").resolve()
    file_path = (base_dir / file_name).resolve()
    
    try:
        if base_dir in file_path.parents or file_path == base_dir:
            if file_path.exists():
                if file_path.is_dir():
                    shutil.rmtree(file_path) 
                else:
                    os.unlink(file_path) 
                return jsonify({'success': True})
            else:
                return jsonify({'success': False, 'error': 'File not found'}), 404
        else:
            return jsonify({'success': False, 'error': 'Unauthorized path access'}), 403
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ─── Packages ───

@app.route("/server/<name>/packages/install", methods=["POST"])
@login_required
@server_access_guard
def install_package(name):
    global SERVER_LOGS
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg: return jsonify({"success": False, "error": "Not found"}), 404
    payload = request.get_json() or {}
    pkg_name = payload.get("name", "").strip()
    pkg_ver = payload.get("version", "").strip()
    if not pkg_name: return jsonify({"success": False, "error": "Package name required"})
    
    extract_dir = SERVERS_DIR / name / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    install_target = f"{pkg_name}=={pkg_ver}" if pkg_ver else pkg_name
    SERVER_LOGS[name] = f"📦 [PIP]: Installing {install_target}...\n"

    def run_installation():
        global SERVER_LOGS
        try:
            cmd = ["pip", "install", install_target]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                
            while True:
                line = proc.stdout.readline()
                if line == '' and proc.poll() is not None:
                    break
                if line:
                    SERVER_LOGS[name] += line
            
            err = proc.stderr.read()
            if err:
                SERVER_LOGS[name] += f"\nℹ️ [LOG/WARN]: {err}"
                
            if proc.returncode == 0:
                SERVER_LOGS[name] += f"\n✅ [SUCCESS]: {install_target} ইনস্টলেশন সফল হয়েছে!"
                
                pkgs = cfg.get("packages", [])
                pkgs = [p for p in pkgs if p["name"] != pkg_name]
                pkgs.append({"name": pkg_name, "version": pkg_ver or "latest", "installed_at": datetime.now().isoformat()})
                cfg["packages"] = pkgs
                
                req_path = extract_dir / "requirements.txt"
                try:
                    lines = req_path.read_text().splitlines() if req_path.exists() else []
                    lines = [l for l in lines if not l.lower().startswith(pkg_name.lower())]
                    lines.append(f"{pkg_name}=={pkg_ver}" if pkg_ver else pkg_name)
                    req_path.write_text("\n".join(lines) + "\n")
                except Exception: pass
                
                data["servers"][name] = cfg
                save_data(data)
            else:
                SERVER_LOGS[name] += f"\n❌ [ERROR]: {install_target} ইনস্টল করতে সমস্যা হয়েছে।"
        except Exception as e:
            SERVER_LOGS[name] += f"\n🚨 [CRITICAL ERROR]: {str(e)}"

    t = threading.Thread(target=run_installation)
    t.start()
        
    return jsonify({'success': True, 'message': f'{install_target} ইনস্টল করার রিকোয়েস্ট পাঠানো হয়েছে। অনুগ্রহ করে টার্মিনালটি লক্ষ্য করুন।'})

@app.route("/server/<name>/packages/remove", methods=["POST"])
@login_required
@server_access_guard
def remove_package(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg: return jsonify({"success": False}), 404
    payload = request.get_json()
    pkg_name = payload.get("name", "")
    
    cfg["packages"] = [p for p in cfg.get("packages", []) if p["name"] != pkg_name]
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})

# ─── Settings ─────────────────────────────────────────────────────────────────

@app.route("/server/<name>/settings", methods=["POST"])
@login_required
@server_access_guard
def save_settings(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg: return jsonify({"success": False, "error": "Not found"}), 404
    payload = request.get_json()
    cfg["main_file"] = payload.get("main_file", cfg.get("main_file", ""))
    cfg["port"] = payload.get("port", cfg.get("port", 8080))
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})

# ─── Auto Reset routes ────────────────────────────────────────────────────────

@app.route("/server/<name>/auto-reset/settings", methods=["POST"])
@login_required
@server_access_guard
def save_auto_reset_settings(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg: return jsonify({"success": False, "error": "Not found"}), 404
    payload = request.get_json()
    enabled = bool(payload.get("enabled", False))
    years = int(payload.get("years", 0) or 0)
    days = int(payload.get("days", 0) or 0)
    hours = int(payload.get("hours", 0) or 0)
    minutes = int(payload.get("minutes", 0) or 0)
    seconds = int(payload.get("seconds", 0) or 0)
    cfg["auto_reset"] = {"enabled": enabled, "years": years, "days": days, "hours": hours, "minutes": minutes, "seconds": seconds}
    data["servers"][name] = cfg
    save_data(data)
    if name in RESET_TIMERS:
        try: RESET_TIMERS[name]["timer"].cancel()
        except Exception: pass
        del RESET_TIMERS[name]
    if enabled:
        total = _auto_reset_seconds(cfg)
        if total > 0: _schedule_reset(name, total)
    return jsonify({"success": True})

@app.route("/server/<name>/auto-reset", methods=["POST"])
@login_required
@server_access_guard
def trigger_auto_reset(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg: return jsonify({"success": False, "error": "Not found"}), 404
    threading.Thread(target=_do_auto_reset, args=[name], daemon=True).start()
    return jsonify({"success": True})

@app.route("/server/<name>/auto-reset/status")
@login_required
def auto_reset_status(name):
    if name in RESET_TIMERS:
        entry = RESET_TIMERS[name]
        started = datetime.fromisoformat(entry["started_at"])
        elapsed = (datetime.now() - started).total_seconds()
        remaining = max(0, entry["total_seconds"] - int(elapsed))
        return jsonify({"remaining": remaining, "total": entry["total_seconds"]})
    data = load_data()
    cfg = data["servers"].get(name, {})
    total = _auto_reset_seconds(cfg)
    return jsonify({"remaining": total, "total": total})

# ─── Start / Stop ───

def read_live_output(process, name):
    global SERVER_LOGS
    while True:
        output = process.stdout.readline()
        if output == '' and process.poll() is not None:
            break
        if output:
            if name not in SERVER_LOGS:
                SERVER_LOGS[name] = ""
            SERVER_LOGS[name] += output
            
    stderr = process.stderr.read()
    if stderr:
        if name not in SERVER_LOGS:
            SERVER_LOGS[name] = ""
        SERVER_LOGS[name] += "\n🚨 [PYTHON CRASH ERROR]:\n" + stderr

@app.route("/server/<name>/start", methods=["POST"])
@login_required
@server_access_guard
def start_server(name):
    global SERVER_LOGS
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg: return jsonify({"success": False, "error": "Not found"}), 404
    pid = cfg.get("pid")
    if pid and is_process_alive(pid): return jsonify({"success": False, "error": "Already running"})
    
    extract_dir = SERVERS_DIR / name / "extracted"
    all_files = os.listdir(extract_dir) if extract_dir.exists() else []

    if not cfg.get("main_file") or cfg["main_file"] not in all_files:
        if "main.py" in all_files: cfg["main_file"] = "main.py"
        else:
            py_files = [f for f in all_files if f.endswith(".py")]
            cfg["main_file"] = py_files[0] if py_files else "main.py"
            
    if not str(cfg["main_file"]).endswith(".py"):
        return jsonify({"success": False, "error": "প্যাকেজ লক অ্যাক্টিভেটেড! পাইথন প্যাকেজে শুধুমাত্র .py ফাইল রান করা সম্ভব।"})

    main_file = cfg["main_file"]
    main_path = extract_dir / main_file
    if not main_path.exists(): return jsonify({"success": False, "error": f"{main_file} not found. Upload your files first."})
    
    log_path = SERVERS_DIR / name / "logs.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    cmd = get_run_command(main_file)
    env = os.environ.copy()
    env["PORT"] = str(cfg.get("port", 8080))
    
    try:
        SERVER_LOGS[name] = ""  
        with open(log_path, "w") as lf:
            lf.write(f"\n{'='*50}\n[{datetime.now().isoformat()}] Starting Fresh Context: {' '.join(cmd)}\n{'='*50}\n")
        log_file = open(log_path, "a")
        
        kwargs = {}
        if os.name != 'nt':
            kwargs['preexec_fn'] = os.setsid
        else:
            kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
            
        proc = subprocess.Popen(
            cmd, cwd=str(extract_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, shell=False, text=True, **kwargs
        )
        
        t = threading.Thread(target=read_live_output, args=(proc, name))
        t.daemon = True
        t.start()
        
        time.sleep(1.0)
        if proc.poll() is not None:
            cfg["status"] = "stopped"
            cfg["pid"] = None
            data["servers"][name] = cfg
            save_data(data)
            return jsonify({"success": False, "error": "বটটি চালু হওয়ার সাথে সাথেই ক্র্যাশ করেছে! আপনার টার্মিনাল লগ (Terminal Logs) চেক করুন।"})

        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
        data["servers"][name] = cfg
        save_data(data)
        return jsonify({"success": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/server/<name>/stop", methods=["POST"])
@login_required
def stop_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg: return jsonify({"success": False}), 404
    pid = cfg.get("pid")
    stopped = False
    if name in RUNNING_PROCESSES:
        entry = RUNNING_PROCESSES[name]
        proc = entry["proc"]
        try:
            if os.name != 'nt':
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            try: proc.terminate()
            except Exception: pass
        try: proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass
        try: entry["log_file"].close()
        except Exception: pass
        del RUNNING_PROCESSES[name]
        stopped = True
    if pid and not stopped: kill_process(pid)
        
    log_path = SERVERS_DIR / name / "logs.txt"
    try:
        with open(log_path, "w") as lf: lf.write("")
    except Exception: pass
        
    cfg["status"] = "stopped"
    cfg["pid"] = None
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})

# ─── Logs ───

@app.route("/server/<name>/logs")
@login_required
@server_access_guard
def get_logs(name):
    global SERVER_LOGS
    log_path = SERVERS_DIR / name / "logs.txt"
    
    if name in SERVER_LOGS and SERVER_LOGS[name]:
        return jsonify({"logs": SERVER_LOGS[name]})
        
    if not log_path.exists(): return jsonify({"logs": ""})
    try:
        content = log_path.read_text(errors="replace")
        lines = content.splitlines()
        if len(lines) > 200:
            lines = lines[-200:]
            content = "\n".join(lines)
        return jsonify({"logs": content})
    except Exception as e:
        return jsonify({"logs": f"Error reading logs: {e}"})

@app.route("/server/<name>/logs/clear", methods=["POST"])
@login_required
@server_access_guard
def clear_logs(name):
    global SERVER_LOGS
    if name in SERVER_LOGS:
        SERVER_LOGS[name] = ""
    log_path = SERVERS_DIR / name / "logs.txt"
    try: log_path.write_text("")
    except Exception: pass
    return jsonify({"success": True})

# ─── Admin ────────────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        return render_template("admin_login.html", error="Wrong admin password")
    return render_template("admin_login.html", error=None)

@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("login"))

@app.route("/admin")
@admin_required
def admin_dashboard():
    data = load_data()
    servers = data["servers"]
    users_raw = data["users"]
    settings = data.get("settings", {})
    
    for name, cfg in servers.items():
        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            
    running = sum(1 for v in servers.values() if v.get("status") == "running")
    total_files = 0
    for sname in servers:
        ed = SERVERS_DIR / sname / "extracted"
        if ed.exists():
            total_files += sum(1 for f in ed.rglob("*") if f.is_file())
            
    user_stats = []
    for u in users_raw:
        u_servers = [v for v in servers.values() if v.get("owner") == u]
        u_files = 0
        for sv in u_servers:
            ed = SERVERS_DIR / sv["name"] / "extracted"
            if ed.exists():
                u_files += sum(1 for f in ed.rglob("*") if f.is_file())
        
        user_stats.append({
            "username": u,
            "password": users_raw[u].get("raw_password", "Not Saved Yet"),
            "projects": len(u_servers),
            "running": sum(1 for sv in u_servers if sv.get("status") == "running"),
            "files": u_files,
            "joined": users_raw[u].get("joined", ""),
            "coins": users_raw[u].get("coins", 0)
        })
        
    return render_template("admin.html", users=user_stats, servers=servers, settings=settings,
                           total_users=len(users_raw), total_projects=len(servers),
                           running=running, total_files=total_files,
                           theme_presets=THEME_PRESETS)

@app.route("/admin/user/<username>/files")
@admin_required
def admin_user_files(username):
    data = load_data()
    user_servers = {k: v for k, v in data["servers"].items() if v.get("owner") == username}
    file_data = {}
    for name, cfg in user_servers.items():
        ed = SERVERS_DIR / name / "extracted"
        file_data[name] = {"config": cfg, "files": list_files(ed)}
    return render_template("admin_files.html", username=username, file_data=file_data)

@app.route("/admin/user/<username>/delete", methods=["POST"])
@admin_required
def admin_delete_user(username):
    data = load_data()
    to_delete = [k for k, v in data["servers"].items() if v.get("owner") == username]
    for name in to_delete:
        pid = data["servers"][name].get("pid")
        if pid: kill_process(pid)
        if name in RUNNING_PROCESSES:
            try: RUNNING_PROCESSES[name]["proc"].terminate()
            except Exception: pass
            del RUNNING_PROCESSES[name]
        if name in RESET_TIMERS:
            try: RESET_TIMERS[name]["timer"].cancel()
            except Exception: pass
            del RESET_TIMERS[name]
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
        del data["servers"][name]
    data["users"].pop(username, None)
    save_data(data)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/maintenance", methods=["POST"])
@admin_required
def toggle_maintenance():
    data = load_data()
    payload = request.get_json()
    data["settings"]["maintenance"] = payload.get("enabled", False)
    data["settings"]["maintenance_msg"] = payload.get("message", "Under maintenance")
    save_data(data)
    return jsonify({"success": True})

@app.route("/admin/theme", methods=["POST"])
@admin_required
def set_theme():
    data = load_data()
    payload = request.get_json()
    color = payload.get("color", "#a855f7").strip()
    if not color.startswith("#"): return jsonify({"success": False, "error": "Invalid color"}), 400
    if "settings" not in data: data["settings"] = {}
    data["settings"]["theme_color"] = color
    save_data(data)
    return jsonify({"success": True, "color": color})

@app.route('/admin/logo', methods=['POST'])
def update_logo():
    data = request.get_json()
    new_url = data.get('logo_url')
    if new_url:
        full_data = load_data() 
        if 'settings' not in full_data: full_data['settings'] = {}
        full_data['settings']['logo_url'] = new_url
        save_data(full_data)
        return jsonify({"success": True, "message": "Logo updated!"})
    return jsonify({"success": False, "message": "Invalid URL"}), 400

@app.route('/admin/music/upload', methods=['POST'])
def upload_music_file():
    try:
        if 'audio' not in request.files: return jsonify({"success": False, "error": "No file part"}), 400
        file = request.files['audio']
        if file.filename == '': return jsonify({"success": False, "error": "No selected file"}), 400

        if file:
            filename = secure_filename(file.filename)
            file_path = os.path.join(UPLOAD_FOLDER, filename)
            file.save(file_path)

            new_song = Song(name=filename)
            db.session.add(new_song)
            db.session.commit()
            
            music_url = f"/static/uploads/{filename}"
            settings_file = 'settings.json'
            current_settings = json.load(open(settings_file)) if os.path.exists(settings_file) else {}
            current_settings['music_url'] = music_url
            with open(settings_file, 'w') as f: json.dump(current_settings, f, indent=4)
                
            return jsonify({"success": True, "url": music_url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/admin/music', methods=['POST'])
def save_music():
    try:
        data = request.get_json()
        music_url = data.get('music_url', '')
        settings_file = 'settings.json'
        current_settings = json.load(open(settings_file)) if os.path.exists(settings_file) else {}
        current_settings['music_url'] = music_url
        with open(settings_file, 'w') as f: json.dump(current_settings, f, indent=4)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/get_songs', methods=['GET'])
def get_songs():
    songs = Song.query.all()
    return jsonify([{'id': song.id, 'name': song.name} for song in songs])

@app.route('/delete_song/<int:id>', methods=['DELETE'])
def delete_song(id):
    song = Song.query.get(id)
    if song:
        file_path = os.path.join('static', 'uploads', song.name)
        if os.path.exists(file_path):
            try: os.remove(file_path)
            except Exception: pass
        db.session.delete(song)
        db.session.commit()
        return jsonify({'message': 'Song and file deleted successfully'})
    return jsonify({'message': 'Song not found'}), 404

@app.route('/admin/songs')
@login_required 
def list_songs():
    try:
        songs = Song.query.all()
        return render_template('admin_songs.html', songs=songs)
    except Exception as e:
        return "Error loading songs", 500

@app.route('/api/all_songs')
def fetch_all_songs_api():
    try:
        songs = Song.query.all()
        return jsonify([{'name': s.name} for s in songs])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ─── Download routes ───────────────────────────────────────────────────────────

@app.route("/admin/file/<project_name>/download")
@admin_required
def admin_download_file(project_name):
    file_path = request.args.get("path", "")
    if not file_path: abort(400)
    safe_path = (SERVERS_DIR / project_name / "extracted" / file_path).resolve()
    base = (SERVERS_DIR / project_name / "extracted").resolve()
    if not str(safe_path).startswith(str(base)) or not safe_path.exists() or safe_path.is_dir(): abort(404)
    return send_file(safe_path, as_attachment=True, download_name=safe_path.name)

@app.route("/admin/project/<project_name>/download")
@admin_required
def admin_download_project(project_name):
    type_filter = request.args.get("type", "all")
    extract_dir = SERVERS_DIR / project_name / "extracted"
    if not extract_dir.exists(): abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in extract_dir.rglob("*"):
            if not f.is_file(): continue
            if type_filter != "all" and not f.name.endswith(type_filter): continue
            zf.write(f, f.relative_to(extract_dir))
    buf.seek(0)
    ext_part = type_filter.replace(".", "") if type_filter != "all" else ""
    return send_file(buf, as_attachment=True, download_name=f"{project_name}{'-' + ext_part if ext_part else ''}.zip", mimetype="application/zip")

@app.route("/admin/user/<username>/download")
@admin_required
def admin_download_user(username):
    type_filter = request.args.get("type", "all")
    data = load_data()
    user_servers = {k: v for k, v in data["servers"].items() if v.get("owner") == username}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in user_servers:
            extract_dir = SERVERS_DIR / name / "extracted"
            if not extract_dir.exists(): continue
            for f in extract_dir.rglob("*"):
                if not f.is_file(): continue
                if type_filter != "all" and not f.name.endswith(type_filter): continue
                zf.write(f, Path(name) / f.relative_to(extract_dir))
    buf.seek(0)
    ext_part = type_filter.replace(".", "") if type_filter != "all" else ""
    return send_file(buf, as_attachment=True, download_name=f"{username}-files{'-' + ext_part if ext_part else ''}.zip", mimetype="application/zip")
       
# ─── New Dynamic API Runner ───────────────────────────────────────────────────

@app.route('/<dynamic_name>')
def dynamic_api_runner(dynamic_name):
    try:
        data = load_data()
        for project_name in data["servers"]:
            if check_server_expiry(project_name, data):
                continue
            project_dir = SERVERS_DIR / project_name / "extracted"
            for ext in [".py", ".json", ".html", ".txt"]:
                file_path = (project_dir / f"{dynamic_name}{ext}").resolve()
                if file_path.exists():
                    if ext == ".py":
                        result = subprocess.run(["python", str(file_path)], capture_output=True, text=True, timeout=30)
                        if result.returncode == 0:
                            try: return jsonify(json.loads(result.stdout))
                            except: return f"<pre>{result.stdout}</pre>"
                        return jsonify({"error": "Script Error", "details": result.stderr}), 500
                    return send_file(file_path)
        return jsonify({"error": f"Route or File '{dynamic_name}' not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
        
# ─── Project Management Routes (Admin Project List) ───────────────────────────

@app.route("/admin/project/control", methods=["POST"])
@admin_required
def admin_project_control():
    try:
        data = request.get_json()
        name = data.get("name")
        action = data.get("action")
        if action == "start": return start_server(name)
        elif action == "stop": return stop_server(name)
        return jsonify({"success": False, "error": "Invalid action"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/admin/project/update_reset", methods=["POST"])
@admin_required
def admin_update_reset():
    try:
        data = request.get_json()
        name = data.get("name")
        minutes = int(data.get("minutes", 0))
        all_data = load_data()
        cfg = all_data["servers"].get(name)
        if cfg:
            cfg["auto_reset"] = {"enabled": True if minutes > 0 else False, "years": 0, "days": 0, "hours": 0, "minutes": minutes, "seconds": 0}
            cfg["reset_time"] = minutes
            all_data["servers"][name] = cfg
            save_data(all_data)
            if name in RESET_TIMERS:
                try: RESET_TIMERS[name]["timer"].cancel()
                except: pass
                del RESET_TIMERS[name]
            if minutes > 0: _schedule_reset(name, minutes * 60)
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Project not found"}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/servers', methods=['GET'])
@login_required
def get_servers():
    username = session.get("username")
    data = load_data()
    
    user_servers = [
        v for v in data["servers"].values() 
        if str(v.get("owner", "")).strip().lower() == username.lower()
    ]
    
    for srv in user_servers:
        pid = srv.get("pid")
        if pid and not is_process_alive(pid):
            srv["status"] = "stopped"
            srv["pid"] = None
            
    return jsonify({"servers": user_servers})

# ─── ফিক্সড রুটস ───────────────────────────────────────────────────────────
@app.route('/premium')
@login_required
def premium(): 
    return redirect(url_for('dashboard')) 

@app.route('/free/')
def free(): return render_template('free.html')

@app.route('/coin-purchase')
def coin_purchase(): return render_template('coin_purchase.html')

@app.route('/free_coin')
def free_coin():
    if 'username' not in session: return redirect(url_for('login'))
    return render_template('free_coin.html')

@app.route("/api/get_user_coins", methods=["GET"])
@login_required
def get_user_coins():
    username = session.get("username")
    data = load_data()
    
    if username not in data["users"]:
        return jsonify({"status": "error", "msg": "User not found."}), 404
        
    user = data["users"][username]
    current_coins = user.get("coins", 0)
    
    return jsonify({
        "status": "success",
        "coins": current_coins
    }), 200


# ─── TELEGRAM BOT CODE INTEGRATION ────────────────────────────────────────────

BOT_TOKEN = "8693088492:AAGTupogl8apbWOoV1VLxntEOU6PRY38pVU"
ADMIN_ID = 8128075446  

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

admin_to_user_routing = {}  
admin_to_webuser_routing = {}  
admin_to_user_email = {}  

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    welcome_text = (
        f"👋 Welcome to Our Website Coin Shop, {user.first_name}!\n\n"
        "Here you can purchase coins easily. "
        "If you want to buy coins, please type your message below or click the 'Buy Coin' button."
    )
    keyboard = [[InlineKeyboardButton("🪙 Buy Coin", callback_data="buy_coin")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.data == "buy_coin":
        await query.message.reply_text(
            "💬 How many coins would you like to buy and what is your registered email? "
            "Please type them here. Our team will contact you shortly."
        )

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    username = update.effective_user.username or "No Username"
    message_text = update.message.text.strip()

    if user_id == ADMIN_ID:
        return

    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', message_text)
    db_info_text = ""
    target_web_user = None
    detected_email = None

    if email_match:
        detected_email = email_match.group(0).lower()
        web_data = load_data()
        
        for web_username, web_user_info in web_data.get("users", {}).items():
            if web_user_info.get("email", "").lower() == detected_email:
                target_web_user = web_username
                db_info_text = (
                    f"✨ **Database Match Found!**\n"
                    f"👤 Account Username: `{web_username}`\n"
                    f"🪙 Current Balance: `{web_user_info.get('coins', 0)}` Coins\n"
                    f"👉 To add coins, reply to this message with just the number (e.g., 50)"
                )
                break
        
        if not db_info_text:
            db_info_text = f"⚠️ Email detected, but no matching account found in the database."

    admin_msg_text = (
        f"📩 **New Message Received!**\n"
        f"👤 Name: {update.effective_user.first_name}\n"
        f"🆔 Telegram ID: `{user_id}`\n"
        f"🌐 Telegram Username: @{username}\n"
        f"💬 Message: {message_text}\n\n"
        f"{db_info_text}"
    )
    
    sent_msg = await context.bot.send_message(
        chat_id=ADMIN_ID, 
        text=admin_msg_text, 
        parse_mode="Markdown"
    )
    
    admin_to_user_routing[sent_msg.message_id] = user_id
    if target_web_user:
        admin_to_webuser_routing[sent_msg.message_id] = target_web_user
    if detected_email:
        admin_to_user_email[sent_msg.message_id] = detected_email
    
    await update.message.reply_text("✅ Your message has been sent to the admin. Please wait for a response.")

async def handle_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    if not update.message.reply_to_message:
        await update.message.reply_text("❌ To reply, please select the user's message and click 'Reply'.")
        return

    replied_msg_id = update.message.reply_to_message.message_id
    target_user_id = admin_to_user_routing.get(replied_msg_id)
    target_web_user = admin_to_webuser_routing.get(replied_msg_id)
    target_email = admin_to_user_email.get(replied_msg_id, "N/A")
    admin_reply_text = update.message.text.strip()

    if target_user_id:
        if admin_reply_text.isdigit() and target_web_user:
            coin_to_add = int(admin_reply_text)
            
            web_data = load_data()
            if target_web_user in web_data["users"]:
                if "coins" not in web_data["users"][target_web_user]:
                    web_data["users"][target_web_user]["coins"] = 0
                    
                web_data["users"][target_web_user]["coins"] += coin_to_add
                current_total = web_data["users"][target_web_user]["coins"]
                save_data(web_data)
                
                success_text = (
                    f"🪙 **Coins Successfully Added!**\n\n"
                    f"Your account has been credited with new coins. Here are the details:\n\n"
                    f"📧 **Email:** `{target_email}`\n"
                    f"👤 **Username:** `{target_web_user}`\n"
                    f"➕ **Added Coins:** `+{coin_to_add}` Coins\n"
                    f"💰 **Total Balance:** `{current_total}` Coins\n\n"
                    f"Thank you for choosing us!"
                )
                await context.bot.send_message(chat_id=target_user_id, text=success_text, parse_mode="Markdown")
                await update.message.reply_text(f"🚀 Successfully added {coin_to_add} coins to `{target_web_user}`'s account. User has been notified.")
                return

        try:
            await context.bot.send_message(
                chat_id=target_user_id, 
                text=f"✉️ **Admin Reply:**\n{admin_reply_text}", 
                parse_mode="Markdown"
            )
            await update.message.reply_text("🚀 Message successfully delivered to the user.")
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to send message. Error: {e}")
    else:
        await update.message.reply_text("❌ Sorry, no user ID associated with this message was found.")


# থ্রেডের ভেতর রান করার জন্য সুনির্দিষ্ট এসিনক্রোনাস ফাংশন
def run_telegram_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_click))
    
    application.add_handler(
        MessageHandler(filters.Chat(ADMIN_ID) & filters.REPLY & filters.TEXT, handle_admin_reply)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Chat(ADMIN_ID), handle_user_message)
    )
    
    print("Telegram Bot is initializing via custom thread loop...")
    
    loop.run_until_complete(application.initialize())
    loop.run_until_complete(application.start())
    # run_polling এর চেয়ে পোলিং লাইফসাইকেল রেলওয়ের জন্য এভাবে হ্যান্ডেল করা ভালো
    loop.run_until_complete(application.updater.start_polling(allowed_updates=Update.ALL_TYPES))
    loop.run_forever()

# ─── Healthcheck Route ───────────────────────────────────────────────────
@app.route('/health')
def railway_healthcheck():
    return "OK", 200

# ─── Server initialization ───────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    # বটের জন্য ব্যাকগ্রাউন্ড থ্রেড চালু
    bot_thread = threading.Thread(target=run_telegram_bot)
    bot_thread.daemon = True
    bot_thread.start()

    # রেলওয়ের পোর্ট রিড করা
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
