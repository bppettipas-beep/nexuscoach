#!/usr/bin/env python3
"""NexusCoach — AI-powered motivational SMS platform."""

import json, os, sqlite3, logging, threading, webbrowser, secrets
from datetime import datetime
from functools import wraps

import requests

from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, g, send_from_directory)
from werkzeug.security import generate_password_hash, check_password_hash
import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(BASE_DIR, 'nexuscoach.db')
SECRET_FILE = os.path.join(BASE_DIR, '.flask_secret')

def _get_secret():
    if os.environ.get('SECRET_KEY'):
        return os.environ['SECRET_KEY']
    if os.path.exists(SECRET_FILE):
        return open(SECRET_FILE).read().strip()
    k = secrets.token_hex(32)
    open(SECRET_FILE, 'w').write(k)
    return k

app = Flask(__name__)
app.secret_key = _get_secret()
scheduler = BackgroundScheduler()

CARRIERS = {
    'att':        ('AT&T',         'txt.att.net'),
    'tmobile':    ('T-Mobile',     'tmomail.net'),
    'verizon':    ('Verizon',      'vtext.com'),
    'sprint':     ('Sprint',       'messaging.sprintpcs.com'),
    'metropcs':   ('Metro PCS',    'mymetropcs.com'),
    'boost':      ('Boost Mobile', 'sms.myboostmobile.com'),
    'cricket':    ('Cricket',      'sms.cricketwireless.net'),
    'uscellular': ('US Cellular',  'email.uscc.net'),
    'googlefi':   ('Google Fi',    'msg.fi.google.com'),
}

STYLES = {
    'gentle':        ('Gentle & Warm',     'Encouraging, supportive nudges'),
    'tough':         ('Tough Love',        'Direct, no excuses, challenging'),
    'inspirational': ('Deeply Inspiring',  'Poetic, profound, uplifting'),
    'practical':     ('Practical',         'Action steps and concrete tips'),
}

@app.template_filter('fromjson')
def fromjson_filter(s):
    try:
        return json.loads(s)
    except Exception:
        return ['08:00']

TIMEZONES = [
    ('US/Eastern',  'Eastern (ET)'),
    ('US/Central',  'Central (CT)'),
    ('US/Mountain', 'Mountain (MT)'),
    ('US/Pacific',  'Pacific (PT)'),
    ('US/Alaska',   'Alaska'),
    ('US/Hawaii',   'Hawaii'),
    ('UTC',         'UTC'),
]

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA journal_mode=WAL')
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db:
        db.close()

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT    UNIQUE NOT NULL,
                password_hash TEXT    NOT NULL,
                name          TEXT    DEFAULT '',
                phone         TEXT    DEFAULT '',
                carrier       TEXT    DEFAULT 'verizon',
                goal          TEXT    DEFAULT '',
                style         TEXT    DEFAULT 'gentle',
                times         TEXT    DEFAULT '["08:00"]',
                freq          TEXT    DEFAULT 'daily',
                tz            TEXT    DEFAULT 'US/Eastern',
                is_admin      INTEGER DEFAULT 0,
                is_active     INTEGER DEFAULT 0,
                plan          TEXT    DEFAULT 'starter',
                msgs_today    INTEGER DEFAULT 0,
                msgs_date     TEXT    DEFAULT '',
                setup_done    INTEGER DEFAULT 0,
                created_at    TEXT    DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS history (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text    TEXT    NOT NULL,
                ok      INTEGER DEFAULT 1,
                sent_at TEXT    DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        """)
        db.commit()

def _raw_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def get_setting(key, default=''):
    # Env vars (NC_CLAUDE_KEY, NC_EJS_SERVICE, etc.) override DB — used on Railway
    env_val = os.environ.get(f'NC_{key.upper()}')
    if env_val:
        return env_val
    with _raw_db() as db:
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default

def set_setting(key, value):
    with _raw_db() as db:
        db.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', (key, value))
        db.commit()

def get_admin_cfg():
    return {
        'claude_key':   get_setting('claude_key'),
        'ejs_service':  get_setting('ejs_service'),
        'ejs_template': get_setting('ejs_template'),
        'ejs_pubkey':   get_setting('ejs_pubkey'),
        'daily_limit':  int(get_setting('daily_limit', '3')),
        'paused':       get_setting('paused', '0') == '1',
    }

# ── Auth ──────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrapped

def admin_required(f):
    @wraps(f)
    def wrapped(*a, **kw):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = get_db().execute('SELECT is_admin FROM users WHERE id=?', (session['user_id'],)).fetchone()
        if not user or not user['is_admin']:
            return redirect(url_for('dashboard'))
        return f(*a, **kw)
    return wrapped

@app.context_processor
def inject_current_user():
    user = None
    if 'user_id' in session:
        try:
            user = get_db().execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
        except Exception:
            pass
    return {'current_user': user}

# ── Core logic ────────────────────────────────────────────────────────────────

def sms_addr(phone, carrier):
    digits = ''.join(filter(str.isdigit, phone))[-10:]
    _, domain = CARRIERS.get(carrier, ('?', 'vtext.com'))
    return f"{digits}@{domain}"

def send_via_emailjs(acfg, to_addr, text):
    resp = requests.post(
        'https://api.emailjs.com/api/v1.0/email/send',
        json={
            'service_id':  acfg['ejs_service'],
            'template_id': acfg['ejs_template'],
            'user_id':     acfg['ejs_pubkey'],
            'template_params': {'to_email': to_addr, 'message': text},
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise Exception(f"EmailJS {resp.status_code}: {resp.text}")

def generate_message(user, acfg):
    client = anthropic.Anthropic(api_key=acfg['claude_key'])
    hour = datetime.now().hour
    tod  = 'morning' if hour < 12 else 'afternoon' if hour < 17 else 'evening'
    style_desc = {
        'gentle':        'warm, gentle, and encouraging',
        'tough':         'direct, no-nonsense, tough-love',
        'inspirational': 'poetic, profound, and deeply uplifting',
        'practical':     'practical, action-oriented, and concrete',
    }.get(user['style'] or 'gentle', 'warm and encouraging')
    prompt = (
        f"Write a motivational SMS for {user['name']}.\n"
        f"Their goal: {user['goal']}\n"
        f"Tone: {style_desc}\n"
        f"Time of day: {tod}\n\n"
        "Rules: under 155 characters, no hashtags, personal to their goal, "
        "just the message text with no labels or quotes."
    )
    resp = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=80,
        messages=[{'role': 'user', 'content': prompt}]
    )
    return resp.content[0].text.strip().strip('"\'')

def do_send_user(user, acfg):
    """Send one scheduled message to a user. Returns (ok, text)."""
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        limit = acfg['daily_limit']
        msgs_today = user['msgs_today'] if user['msgs_date'] == today else 0
        if msgs_today >= limit:
            return False, f'Daily limit ({limit}) reached'

        text = generate_message(user, acfg)
        addr = sms_addr(user['phone'], user['carrier'])
        send_via_emailjs(acfg, addr, text)

        with _raw_db() as db:
            db.execute('UPDATE users SET msgs_today=?, msgs_date=? WHERE id=?',
                       (msgs_today + 1, today, user['id']))
            db.execute('INSERT INTO history (user_id,text,ok) VALUES (?,?,1)',
                       (user['id'], text))
            db.commit()

        logger.info(f"Sent → {user['name']}: {text[:60]}")
        return True, text

    except Exception as e:
        with _raw_db() as db:
            db.execute('INSERT INTO history (user_id,text,ok) VALUES (?,?,0)',
                       (user['id'], str(e)))
            db.commit()
        logger.error(f"Failed → {user['email']}: {e}")
        return False, str(e)

def check_and_send():
    """Scheduler job: every 60s — send messages to eligible users."""
    acfg = get_admin_cfg()
    if acfg['paused'] or not acfg['claude_key'] or not acfg['ejs_service']:
        return

    with _raw_db() as db:
        users = db.execute('SELECT * FROM users WHERE is_active=1 AND setup_done=1').fetchall()

    for user in users:
        try:
            tz = pytz.timezone(user['tz'] or 'US/Eastern')
        except Exception:
            tz = pytz.timezone('US/Eastern')

        now_local    = datetime.now(tz)
        cur_min      = now_local.strftime('%H:%M')
        cur_dow      = now_local.strftime('%a').lower()
        is_weekday   = cur_dow not in ('sat', 'sun')

        times = json.loads(user['times'] or '["08:00"]')
        if cur_min not in times:
            continue

        freq = user['freq'] or 'daily'
        if freq == 'weekdays' and not is_weekday:
            continue
        if freq == 'weekends' and is_weekday:
            continue

        do_send_user(dict(user), acfg)

# ── Static icons ──────────────────────────────────────────────────────────────

@app.route('/icons/<path:filename>')
def icons(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'icons'), filename)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def landing():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('landing.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '').strip()
        if not name or not email or not password:
            error = 'All fields are required.'
        elif len(password) < 6:
            error = 'Password must be at least 6 characters.'
        else:
            db = get_db()
            if db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
                error = 'An account with that email already exists.'
            else:
                is_first = not db.execute('SELECT id FROM users LIMIT 1').fetchone()
                db.execute(
                    'INSERT INTO users (email,password_hash,name,is_admin,is_active) VALUES (?,?,?,?,?)',
                    (email, generate_password_hash(password), name,
                     1 if is_first else 0,
                     1 if is_first else 0)
                )
                db.commit()
                user = db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
                session['user_id'] = user['id']
                return redirect(url_for('setup'))
    return render_template('signup.html', error=error)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '').strip()
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        if not user or not check_password_hash(user['password_hash'], password):
            error = 'Invalid email or password.'
        else:
            session['user_id'] = user['id']
            return redirect(url_for('dashboard'))
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('landing'))

@app.route('/setup', methods=['GET', 'POST'])
@login_required
def setup():
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if request.method == 'POST':
        f     = request.form
        times = [t.strip() for t in f.getlist('times') if t.strip()]
        db.execute("""
            UPDATE users
            SET phone=?, carrier=?, goal=?, style=?, times=?, freq=?, tz=?, setup_done=1
            WHERE id=?
        """, (
            f.get('phone', '').strip(),
            f.get('carrier', 'verizon'),
            f.get('goal', '').strip(),
            f.get('style', 'gentle'),
            json.dumps(times or ['08:00']),
            f.get('freq', 'daily'),
            f.get('tz', 'US/Eastern'),
            user['id']
        ))
        db.commit()
        return redirect(url_for('dashboard'))
    return render_template('setup.html',
        user=user, carriers=CARRIERS, styles=STYLES, timezones=TIMEZONES)

@app.route('/dashboard')
@login_required
def dashboard():
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user['setup_done']:
        return redirect(url_for('setup'))
    hist = db.execute("""
        SELECT text, ok,
               strftime('%m/%d  %I:%M %p', sent_at) AS sent_fmt
        FROM history WHERE user_id=? ORDER BY sent_at DESC LIMIT 20
    """, (user['id'],)).fetchall()
    acfg  = get_admin_cfg()
    ready = bool(acfg['claude_key'] and acfg['ejs_service'] and acfg['ejs_template'] and acfg['ejs_pubkey'])
    times = json.loads(user['times'] or '["08:00"]')
    today = datetime.now().strftime('%Y-%m-%d')
    msgs_today = user['msgs_today'] if user['msgs_date'] == today else 0
    return render_template('dashboard.html',
        user=user, hist=hist, ready=ready, times=times,
        styles=STYLES, msgs_today=msgs_today, limit=acfg['daily_limit'])

@app.route('/test', methods=['POST'])
@login_required
def test_message():
    db   = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user['setup_done']:
        return jsonify({'ok': False, 'error': 'Complete setup first'})
    if not user['is_active']:
        return jsonify({'ok': False, 'error': 'Account not yet activated'})
    acfg = get_admin_cfg()
    if not acfg['claude_key'] or not acfg['gmail']:
        return jsonify({'ok': False, 'error': 'Admin config not complete yet'})
    ok, text = do_send_user(dict(user), acfg)
    return jsonify({'ok': ok, 'message': text if ok else None, 'error': text if not ok else None})

# ── Admin routes ──────────────────────────────────────────────────────────────

@app.route('/admin')
@admin_required
def admin():
    acfg  = get_admin_cfg()
    db    = get_db()
    users = db.execute("""
        SELECT id, name, email, is_admin, is_active, setup_done,
               msgs_today, msgs_date,
               strftime('%m/%d/%Y', created_at) AS joined
        FROM users ORDER BY created_at DESC
    """).fetchall()
    today = datetime.now().strftime('%Y-%m-%d')
    return render_template('admin.html', cfg=acfg, users=users, today=today)

@app.route('/admin/config', methods=['POST'])
@admin_required
def admin_config():
    f = request.form
    for key in ('claude_key', 'ejs_service', 'ejs_template', 'ejs_pubkey'):
        val = f.get(key, '').strip()
        if val:
            set_setting(key, val)
    limit = f.get('daily_limit', '').strip()
    if limit.isdigit() and 1 <= int(limit) <= 20:
        set_setting('daily_limit', limit)
    set_setting('paused', '1' if f.get('paused') else '0')
    return redirect(url_for('admin'))

@app.route('/admin/users/<int:uid>/activate', methods=['POST'])
@admin_required
def activate_user(uid):
    get_db().execute('UPDATE users SET is_active=1 WHERE id=?', (uid,))
    get_db().commit()
    return redirect(url_for('admin'))

@app.route('/admin/users/<int:uid>/deactivate', methods=['POST'])
@admin_required
def deactivate_user(uid):
    get_db().execute('UPDATE users SET is_active=0 WHERE id=?', (uid,))
    get_db().commit()
    return redirect(url_for('admin'))

@app.route('/admin/users/<int:uid>/delete', methods=['POST'])
@admin_required
def delete_user(uid):
    db = get_db()
    db.execute('DELETE FROM history WHERE user_id=?', (uid,))
    db.execute('DELETE FROM users WHERE id=?', (uid,))
    db.commit()
    return redirect(url_for('admin'))

# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    scheduler.add_job(check_and_send, 'interval', seconds=60, id='msg_job')
    scheduler.start()

    port     = int(os.environ.get('PORT', 5000))
    is_local = not os.environ.get('RAILWAY_ENVIRONMENT')

    if is_local:
        def _open():
            import time; time.sleep(1.2)
            webbrowser.open(f'http://localhost:{port}')
        threading.Thread(target=_open, daemon=True).start()
        logger.info(f"NexusCoach → http://localhost:{port}")

    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
