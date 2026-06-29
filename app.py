#!/usr/bin/env python3
"""NexusCoach — AI-powered motivational SMS platform."""

import json, os, logging, threading, webbrowser, secrets
from datetime import datetime
from functools import wraps

import requests
from flask import (Flask, render_template, request, redirect, url_for,
                   session, jsonify, g, send_from_directory)
from werkzeug.security import generate_password_hash, check_password_hash
import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
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

# ── Database ──────────────────────────────────────────────────────────────────

_raw_url = os.environ.get('DATABASE_URL',
                          f'sqlite:///{os.path.join(BASE_DIR, "nexuscoach.db")}')
if _raw_url.startswith('postgres://'):          # Railway gives postgres://, SQLAlchemy needs postgresql://
    _raw_url = _raw_url.replace('postgres://', 'postgresql://', 1)

IS_PG  = _raw_url.startswith('postgresql')
engine = create_engine(_raw_url, pool_pre_ping=True)

def get_db():
    if 'db' not in g:
        g.db = engine.connect()
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db:
        db.close()

def _conn():
    """Raw connection for scheduler (outside request context)."""
    return engine.connect()

def init_db():
    pk  = 'SERIAL'        if IS_PG else 'INTEGER'
    ts  = 'NOW()'         if IS_PG else "(datetime('now'))"
    aipk = 'SERIAL PRIMARY KEY' if IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    with engine.connect() as c:
        c.execute(text(f"""
            CREATE TABLE IF NOT EXISTS users (
                id            {aipk},
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
                created_at    TEXT    DEFAULT {ts}
            )
        """))
        c.execute(text("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT DEFAULT ''
            )
        """))
        c.execute(text(f"""
            CREATE TABLE IF NOT EXISTS history (
                id      {aipk},
                user_id INTEGER NOT NULL,
                text    TEXT    NOT NULL,
                ok      INTEGER DEFAULT 1,
                sent_at TEXT    DEFAULT {ts}
            )
        """))
        c.commit()

def _upsert_setting(c, key, value):
    if IS_PG:
        c.execute(text("""
            INSERT INTO settings (key,value) VALUES (:k,:v)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
        """), {'k': key, 'v': value})
    else:
        c.execute(text("INSERT OR REPLACE INTO settings (key,value) VALUES (:k,:v)"),
                  {'k': key, 'v': value})

def get_setting(key, default=''):
    env_val = os.environ.get(f'NC_{key.upper()}')
    if env_val:
        return env_val
    try:
        with _conn() as c:
            row = c.execute(text('SELECT value FROM settings WHERE key=:k'), {'k': key}).fetchone()
            return row[0] if row else default
    except Exception:
        return default

def set_setting(key, value):
    with _conn() as c:
        _upsert_setting(c, key, value)
        c.commit()

def get_admin_cfg():
    return {
        'claude_key':   get_setting('claude_key'),
        'ejs_service':  get_setting('ejs_service'),
        'ejs_template': get_setting('ejs_template'),
        'ejs_pubkey':   get_setting('ejs_pubkey'),
        'daily_limit':  int(get_setting('daily_limit', '3')),
        'paused':       get_setting('paused', '0') == '1',
    }

def fmt_time(sent_at):
    if not sent_at:
        return ''
    if isinstance(sent_at, str):
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
            try:
                sent_at = datetime.strptime(sent_at, fmt)
                break
            except ValueError:
                continue
        else:
            return str(sent_at)
    return sent_at.strftime('%m/%d  %I:%M %p')

# ── Constants ─────────────────────────────────────────────────────────────────

CARRIERS = {
    # US carriers
    'att':        ('AT&T',              'txt.att.net'),
    'tmobile':    ('T-Mobile',          'tmomail.net'),
    'verizon':    ('Verizon',           'vtext.com'),
    'sprint':     ('Sprint',            'messaging.sprintpcs.com'),
    'metropcs':   ('Metro PCS',         'mymetropcs.com'),
    'boost':      ('Boost Mobile',      'sms.myboostmobile.com'),
    'cricket':    ('Cricket',           'sms.cricketwireless.net'),
    'uscellular': ('US Cellular',       'email.uscc.net'),
    'googlefi':   ('Google Fi',         'msg.fi.google.com'),
    'mint':       ('Mint Mobile',       'tmomail.net'),
    'visible':    ('Visible',           'vtext.com'),
    'xfinity':    ('Xfinity Mobile',    'vtext.com'),
    'consumer':   ('Consumer Cellular', 'mailmymobile.net'),
    'republic':   ('Republic Wireless', 'text.republicwireless.com'),
    # Canadian carriers
    'rogers':     ('Rogers',            'pcs.rogers.com'),
    'bell':       ('Bell',              'txt.bell.ca'),
    'telus':      ('Telus',             'msg.telus.com'),
    'publicmobile': ('Public Mobile',   'msg.telus.com'),
    'freedom':    ('Freedom Mobile',    'txt.freedommobile.ca'),
    'fido':       ('Fido',              'fido.ca'),
    'koodo':      ('Koodo',             'msg.koodomobile.com'),
    'virgin':     ('Virgin Plus',       'vmobile.ca'),
    'videotron':  ('Videotron',         'sms.videotron.ca'),
    'sasktel':    ('SaskTel',           'sms.sasktel.com'),
}

STYLES = {
    'gentle':        ('Gentle & Warm',    'Encouraging, supportive nudges'),
    'tough':         ('Tough Love',       'Direct, no excuses, challenging'),
    'inspirational': ('Deeply Inspiring', 'Poetic, profound, uplifting'),
    'practical':     ('Practical',        'Action steps and concrete tips'),
}

TIMEZONES = [
    ('US/Eastern',  'Eastern (ET)'),
    ('US/Central',  'Central (CT)'),
    ('US/Mountain', 'Mountain (MT)'),
    ('US/Pacific',  'Pacific (PT)'),
    ('US/Alaska',   'Alaska'),
    ('US/Hawaii',   'Hawaii'),
    ('UTC',         'UTC'),
]

@app.template_filter('fromjson')
def fromjson_filter(s):
    try:
        return json.loads(s)
    except Exception:
        return ['08:00']

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
        db  = get_db()
        row = db.execute(text('SELECT is_admin FROM users WHERE id=:id'),
                         {'id': session['user_id']}).fetchone()
        if not row or not row[0]:
            return redirect(url_for('dashboard'))
        return f(*a, **kw)
    return wrapped

@app.context_processor
def inject_current_user():
    user = None
    if 'user_id' in session:
        try:
            row = get_db().execute(text('SELECT * FROM users WHERE id=:id'),
                                   {'id': session['user_id']}).mappings().fetchone()
            user = dict(row) if row else None
        except Exception:
            pass
    return {'current_user': user}

# ── Core logic ────────────────────────────────────────────────────────────────

def sms_addr(phone, carrier):
    digits = ''.join(filter(str.isdigit, phone))[-10:]
    _, domain = CARRIERS.get(carrier, ('?', 'vtext.com'))
    return f"{digits}@{domain}"

def send_via_emailjs(acfg, to_addr, text_body):
    resp = requests.post(
        'https://api.emailjs.com/api/v1.0/email/send',
        json={
            'service_id':  acfg['ejs_service'],
            'template_id': acfg['ejs_template'],
            'user_id':     acfg['ejs_pubkey'],
            'template_params': {'to_email': to_addr, 'message': text_body},
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise Exception(f"EmailJS {resp.status_code}: {resp.text}")

def generate_message(user, acfg):
    client = anthropic.Anthropic(api_key=acfg['claude_key'])
    hour   = datetime.now().hour
    tod    = 'morning' if hour < 12 else 'afternoon' if hour < 17 else 'evening'
    style_desc = {
        'gentle':        'warm, gentle, and encouraging',
        'tough':         'direct, no-nonsense, tough-love',
        'inspirational': 'poetic, profound, and deeply uplifting',
        'practical':     'practical, action-oriented, and concrete',
    }.get(user.get('style') or 'gentle', 'warm and encouraging')
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
    try:
        today      = datetime.now().strftime('%Y-%m-%d')
        limit      = acfg['daily_limit']
        msgs_today = user['msgs_today'] if user.get('msgs_date') == today else 0
        if msgs_today >= limit:
            return False, f'Daily limit ({limit}) reached'

        msg  = generate_message(user, acfg)
        addr = sms_addr(user['phone'], user['carrier'])
        send_via_emailjs(acfg, addr, msg)

        with _conn() as c:
            c.execute(text('UPDATE users SET msgs_today=:n, msgs_date=:d WHERE id=:id'),
                      {'n': msgs_today + 1, 'd': today, 'id': user['id']})
            c.execute(text('INSERT INTO history (user_id,text,ok) VALUES (:u,:t,1)'),
                      {'u': user['id'], 't': msg})
            c.commit()

        logger.info(f"Sent → {user['name']}: {msg[:60]}")
        return True, msg

    except Exception as e:
        with _conn() as c:
            c.execute(text('INSERT INTO history (user_id,text,ok) VALUES (:u,:t,0)'),
                      {'u': user['id'], 't': str(e)})
            c.commit()
        logger.error(f"Failed → {user.get('email')}: {e}")
        return False, str(e)

def check_and_send():
    acfg = get_admin_cfg()
    if acfg['paused'] or not acfg['claude_key'] or not acfg['ejs_service']:
        return
    with _conn() as c:
        rows = c.execute(text('SELECT * FROM users WHERE is_active=1 AND setup_done=1')).mappings().fetchall()
    users = [dict(r) for r in rows]
    for user in users:
        try:
            tz = pytz.timezone(user.get('tz') or 'US/Eastern')
        except Exception:
            tz = pytz.timezone('US/Eastern')
        now_local  = datetime.now(tz)
        cur_min    = now_local.strftime('%H:%M')
        cur_dow    = now_local.strftime('%a').lower()
        is_weekday = cur_dow not in ('sat', 'sun')
        times = json.loads(user.get('times') or '["08:00"]')
        if cur_min not in times:
            continue
        freq = user.get('freq') or 'daily'
        if freq == 'weekdays' and not is_weekday:
            continue
        if freq == 'weekends' and is_weekday:
            continue
        do_send_user(user, acfg)

# ── Static ────────────────────────────────────────────────────────────────────

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
            if db.execute(text('SELECT id FROM users WHERE email=:e'), {'e': email}).fetchone():
                error = 'An account with that email already exists.'
            else:
                is_first = not db.execute(text('SELECT id FROM users LIMIT 1')).fetchone()
                db.execute(text("""
                    INSERT INTO users (email,password_hash,name,is_admin,is_active)
                    VALUES (:e,:p,:n,:a,:ac)
                """), {
                    'e': email, 'p': generate_password_hash(password),
                    'n': name,  'a': 1 if is_first else 0,
                    'ac': 1 if is_first else 0,
                })
                db.commit()
                uid = db.execute(text('SELECT id FROM users WHERE email=:e'), {'e': email}).fetchone()[0]
                session['user_id'] = uid
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
        db  = get_db()
        row = db.execute(text('SELECT * FROM users WHERE email=:e'), {'e': email}).mappings().fetchone()
        if not row or not check_password_hash(row['password_hash'], password):
            error = 'Invalid email or password.'
        else:
            session['user_id'] = row['id']
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
    user = db.execute(text('SELECT * FROM users WHERE id=:id'),
                      {'id': session['user_id']}).mappings().fetchone()
    user = dict(user) if user else {}
    if request.method == 'POST':
        f     = request.form
        times = [t.strip() for t in f.getlist('times') if t.strip()]
        db.execute(text("""
            UPDATE users SET phone=:ph, carrier=:ca, goal=:go, style=:st,
                             times=:ti, freq=:fr, tz=:tz, setup_done=1
            WHERE id=:id
        """), {
            'ph': f.get('phone','').strip(), 'ca': f.get('carrier','verizon'),
            'go': f.get('goal','').strip(),  'st': f.get('style','gentle'),
            'ti': json.dumps(times or ['08:00']), 'fr': f.get('freq','daily'),
            'tz': f.get('tz','US/Eastern'),  'id': session['user_id'],
        })
        db.commit()
        return redirect(url_for('dashboard'))
    return render_template('setup.html',
        user=user, carriers=CARRIERS, styles=STYLES, timezones=TIMEZONES)

@app.route('/dashboard')
@login_required
def dashboard():
    db   = get_db()
    user = db.execute(text('SELECT * FROM users WHERE id=:id'),
                      {'id': session['user_id']}).mappings().fetchone()
    if not user:
        session.clear()
        return redirect(url_for('login'))
    user = dict(user)
    hist_rows = db.execute(text("""
        SELECT text, ok, sent_at FROM history
        WHERE user_id=:uid ORDER BY sent_at DESC LIMIT 20
    """), {'uid': user['id']}).fetchall()
    hist  = [{'text': r[0], 'ok': r[1], 'sent_fmt': fmt_time(r[2])} for r in hist_rows]
    acfg  = get_admin_cfg()
    ready = bool(acfg['claude_key'] and acfg['ejs_service'] and acfg['ejs_template'] and acfg['ejs_pubkey'])
    today = datetime.now().strftime('%Y-%m-%d')
    msgs_today = user.get('msgs_today', 0) if user.get('msgs_date') == today else 0
    times = json.loads(user.get('times') or '["08:00"]')
    return render_template('dashboard.html',
        user=user, hist=hist, ready=ready, times=times,
        styles=STYLES, msgs_today=msgs_today, limit=acfg['daily_limit'])

@app.route('/test', methods=['POST'])
@login_required
def test_message():
    db   = get_db()
    user = db.execute(text('SELECT * FROM users WHERE id=:id'),
                      {'id': session['user_id']}).mappings().fetchone()
    user = dict(user) if user else {}
    if not user.get('setup_done'):
        return jsonify({'ok': False, 'error': 'Complete setup first'})
    acfg = get_admin_cfg()
    if not acfg['claude_key'] or not acfg['ejs_service']:
        return jsonify({'ok': False, 'error': 'Admin config not complete yet'})
    ok, txt = do_send_user(user, acfg)
    return jsonify({'ok': ok, 'message': txt if ok else None, 'error': txt if not ok else None})

@app.route('/admin')
@admin_required
def admin():
    acfg  = get_admin_cfg()
    db    = get_db()
    users = db.execute(text("""
        SELECT id, name, email, is_admin, is_active, setup_done,
               msgs_today, msgs_date, created_at
        FROM users ORDER BY created_at DESC
    """)).mappings().fetchall()
    users = [dict(u) for u in users]
    today = datetime.now().strftime('%Y-%m-%d')
    for u in users:
        u['joined'] = fmt_time(u.get('created_at')) or '—'
    return render_template('admin.html', cfg=acfg, users=users, today=today)

@app.route('/admin/config', methods=['POST'])
@admin_required
def admin_config():
    f  = request.form
    db = get_db()
    for key in ('claude_key', 'ejs_service', 'ejs_template', 'ejs_pubkey'):
        val = f.get(key, '').strip()
        if val:
            _upsert_setting(db, key, val)
    limit = f.get('daily_limit', '').strip()
    if limit.isdigit() and 1 <= int(limit) <= 20:
        _upsert_setting(db, 'daily_limit', limit)
    _upsert_setting(db, 'paused', '1' if f.get('paused') else '0')
    db.commit()
    return redirect(url_for('admin'))

@app.route('/admin/users/<int:uid>/activate', methods=['POST'])
@admin_required
def activate_user(uid):
    db = get_db()
    db.execute(text('UPDATE users SET is_active=1 WHERE id=:id'), {'id': uid})
    db.commit()
    return redirect(url_for('admin'))

@app.route('/admin/users/<int:uid>/deactivate', methods=['POST'])
@admin_required
def deactivate_user(uid):
    db = get_db()
    db.execute(text('UPDATE users SET is_active=0 WHERE id=:id'), {'id': uid})
    db.commit()
    return redirect(url_for('admin'))

@app.route('/admin/users/<int:uid>/delete', methods=['POST'])
@admin_required
def delete_user(uid):
    db = get_db()
    db.execute(text('DELETE FROM history WHERE user_id=:id'), {'id': uid})
    db.execute(text('DELETE FROM users WHERE id=:id'), {'id': uid})
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
