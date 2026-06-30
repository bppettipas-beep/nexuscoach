#!/usr/bin/env python3
"""NexusCoach — AI-powered motivational SMS platform."""

import json, os, logging, threading, webbrowser, secrets, random, hashlib
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

def migrate_db():
    new_cols = [
        ('intensity',       'INTEGER DEFAULT 50'),
        ('q_wakeup',        "TEXT DEFAULT ''"),
        ('q_motivation',    "TEXT DEFAULT ''"),
        ('q_obstacle',      "TEXT DEFAULT ''"),
        ('q_lifestyle',     "TEXT DEFAULT ''"),
        ('q_push',          "TEXT DEFAULT ''"),
        ('phone_verified',  'INTEGER DEFAULT 0'),
        ('verify_code',     "TEXT DEFAULT ''"),
        ('profile_json',    "TEXT DEFAULT '{}'"),
        ('reset_code',      "TEXT DEFAULT ''"),
        ('q_motivator',     "TEXT DEFAULT ''"),
        ('q_identity',      "TEXT DEFAULT ''"),
        ('q_history',       "TEXT DEFAULT ''"),
        ('q_stress',        "TEXT DEFAULT ''"),
        ('q_energy',        "TEXT DEFAULT ''"),
        ('q_sleep',         "TEXT DEFAULT ''"),
        ('q_support',       "TEXT DEFAULT ''"),
        ('q_fear',          "TEXT DEFAULT ''"),
    ]
    with engine.connect() as c:
        pv_added = False
        for col, typedef in new_cols:
            try:
                if IS_PG:
                    c.execute(text(f'ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {typedef}'))
                else:
                    c.execute(text(f'ALTER TABLE users ADD COLUMN {col} {typedef}'))
                if col == 'phone_verified':
                    pv_added = True
            except Exception:
                pass
        if pv_added:
            # Existing users with a phone are already verified — don't lock them out
            c.execute(text("UPDATE users SET phone_verified=1 WHERE phone IS NOT NULL AND phone != ''"))
        c.commit()

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
        'claude_key':    get_setting('claude_key'),
        'twilio_sid':    get_setting('twilio_sid'),
        'twilio_token':  get_setting('twilio_token'),
        'twilio_from':   get_setting('twilio_from'),
        'daily_limit':   int(get_setting('daily_limit', '3')),
        'paused':        get_setting('paused', '0') == '1',
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
    # ── United States ──
    ('US/Eastern',              'US — Eastern (ET)'),
    ('US/Central',              'US — Central (CT)'),
    ('US/Mountain',             'US — Mountain (MT)'),
    ('US/Pacific',              'US — Pacific (PT)'),
    ('US/Alaska',               'US — Alaska'),
    ('US/Hawaii',               'US — Hawaii'),
    ('US/Arizona',              'US — Arizona (no DST)'),
    # ── Canada ──
    ('Canada/Atlantic',         'Canada — Atlantic (AT)'),
    ('Canada/Eastern',          'Canada — Eastern (ET)'),
    ('Canada/Central',          'Canada — Central (CT)'),
    ('Canada/Mountain',         'Canada — Mountain (MT)'),
    ('Canada/Pacific',          'Canada — Pacific (PT)'),
    ('Canada/Newfoundland',     'Canada — Newfoundland'),
    # ── Latin America ──
    ('America/Mexico_City',     'Mexico — Mexico City'),
    ('America/Monterrey',       'Mexico — Monterrey'),
    ('America/Tijuana',         'Mexico — Tijuana'),
    ('America/Bogota',          'Colombia — Bogotá'),
    ('America/Lima',            'Peru — Lima'),
    ('America/Santiago',        'Chile — Santiago'),
    ('America/Buenos_Aires',    'Argentina — Buenos Aires'),
    ('America/Sao_Paulo',       'Brazil — São Paulo'),
    ('America/Manaus',          'Brazil — Manaus'),
    ('America/Caracas',         'Venezuela — Caracas'),
    ('America/La_Paz',          'Bolivia — La Paz'),
    ('America/Asuncion',        'Paraguay — Asunción'),
    ('America/Montevideo',      'Uruguay — Montevideo'),
    ('America/Guayaquil',       'Ecuador — Guayaquil'),
    ('America/Havana',          'Cuba — Havana'),
    ('America/Santo_Domingo',   'Dominican Republic'),
    ('America/Jamaica',         'Jamaica'),
    ('America/Panama',          'Panama'),
    ('America/Costa_Rica',      'Costa Rica'),
    ('America/Guatemala',       'Guatemala'),
    ('America/El_Salvador',     'El Salvador'),
    ('America/Tegucigalpa',     'Honduras'),
    ('America/Managua',         'Nicaragua'),
    # ── Europe ──
    ('Europe/London',           'UK — London (GMT/BST)'),
    ('Europe/Dublin',           'Ireland — Dublin'),
    ('Europe/Lisbon',           'Portugal — Lisbon'),
    ('Europe/Paris',            'France — Paris (CET)'),
    ('Europe/Berlin',           'Germany — Berlin'),
    ('Europe/Madrid',           'Spain — Madrid'),
    ('Europe/Rome',             'Italy — Rome'),
    ('Europe/Amsterdam',        'Netherlands — Amsterdam'),
    ('Europe/Brussels',         'Belgium — Brussels'),
    ('Europe/Zurich',           'Switzerland — Zurich'),
    ('Europe/Vienna',           'Austria — Vienna'),
    ('Europe/Warsaw',           'Poland — Warsaw'),
    ('Europe/Prague',           'Czech Republic — Prague'),
    ('Europe/Budapest',         'Hungary — Budapest'),
    ('Europe/Stockholm',        'Sweden — Stockholm'),
    ('Europe/Oslo',             'Norway — Oslo'),
    ('Europe/Copenhagen',       'Denmark — Copenhagen'),
    ('Europe/Helsinki',         'Finland — Helsinki'),
    ('Europe/Athens',           'Greece — Athens'),
    ('Europe/Bucharest',        'Romania — Bucharest'),
    ('Europe/Sofia',            'Bulgaria — Sofia'),
    ('Europe/Belgrade',         'Serbia — Belgrade'),
    ('Europe/Zagreb',           'Croatia — Zagreb'),
    ('Europe/Kiev',             'Ukraine — Kyiv'),
    ('Europe/Moscow',           'Russia — Moscow'),
    ('Europe/Kaliningrad',      'Russia — Kaliningrad'),
    ('Europe/Samara',           'Russia — Samara'),
    ('Europe/Yekaterinburg',    'Russia — Yekaterinburg'),
    ('Europe/Istanbul',         'Turkey — Istanbul'),
    # ── Africa ──
    ('Africa/Cairo',            'Egypt — Cairo'),
    ('Africa/Johannesburg',     'South Africa — Johannesburg'),
    ('Africa/Lagos',            'Nigeria — Lagos'),
    ('Africa/Nairobi',          'Kenya — Nairobi'),
    ('Africa/Accra',            'Ghana — Accra'),
    ('Africa/Casablanca',       'Morocco — Casablanca'),
    ('Africa/Addis_Ababa',      'Ethiopia — Addis Ababa'),
    ('Africa/Dar_es_Salaam',    'Tanzania — Dar es Salaam'),
    ('Africa/Kampala',          'Uganda — Kampala'),
    ('Africa/Khartoum',         'Sudan — Khartoum'),
    ('Africa/Tunis',            'Tunisia — Tunis'),
    ('Africa/Algiers',          'Algeria — Algiers'),
    # ── Middle East ──
    ('Asia/Dubai',              'UAE — Dubai'),
    ('Asia/Riyadh',             'Saudi Arabia — Riyadh'),
    ('Asia/Kuwait',             'Kuwait'),
    ('Asia/Qatar',              'Qatar — Doha'),
    ('Asia/Bahrain',            'Bahrain'),
    ('Asia/Muscat',             'Oman — Muscat'),
    ('Asia/Baghdad',            'Iraq — Baghdad'),
    ('Asia/Tehran',             'Iran — Tehran'),
    ('Asia/Jerusalem',          'Israel — Jerusalem'),
    ('Asia/Beirut',             'Lebanon — Beirut'),
    ('Asia/Amman',              'Jordan — Amman'),
    ('Asia/Damascus',           'Syria — Damascus'),
    ('Asia/Aden',               'Yemen — Aden'),
    # ── South Asia ──
    ('Asia/Karachi',            'Pakistan — Karachi'),
    ('Asia/Kolkata',            'India — Kolkata (IST)'),
    ('Asia/Dhaka',              'Bangladesh — Dhaka'),
    ('Asia/Colombo',            'Sri Lanka — Colombo'),
    ('Asia/Kathmandu',          'Nepal — Kathmandu'),
    ('Asia/Kabul',              'Afghanistan — Kabul'),
    # ── East & Southeast Asia ──
    ('Asia/Shanghai',           'China — Shanghai (CST)'),
    ('Asia/Hong_Kong',          'Hong Kong'),
    ('Asia/Taipei',             'Taiwan — Taipei'),
    ('Asia/Tokyo',              'Japan — Tokyo'),
    ('Asia/Seoul',              'South Korea — Seoul'),
    ('Asia/Singapore',          'Singapore'),
    ('Asia/Kuala_Lumpur',       'Malaysia — Kuala Lumpur'),
    ('Asia/Jakarta',            'Indonesia — Jakarta'),
    ('Asia/Makassar',           'Indonesia — Makassar'),
    ('Asia/Jayapura',           'Indonesia — Jayapura'),
    ('Asia/Manila',             'Philippines — Manila'),
    ('Asia/Bangkok',            'Thailand — Bangkok'),
    ('Asia/Ho_Chi_Minh',        'Vietnam — Ho Chi Minh City'),
    ('Asia/Hanoi',              'Vietnam — Hanoi'),
    ('Asia/Phnom_Penh',         'Cambodia — Phnom Penh'),
    ('Asia/Rangoon',            'Myanmar — Yangon'),
    ('Asia/Vientiane',          'Laos — Vientiane'),
    ('Asia/Ulaanbaatar',        'Mongolia — Ulaanbaatar'),
    ('Asia/Almaty',             'Kazakhstan — Almaty'),
    ('Asia/Tashkent',           'Uzbekistan — Tashkent'),
    # ── Oceania ──
    ('Australia/Sydney',        'Australia — Sydney (AEST)'),
    ('Australia/Melbourne',     'Australia — Melbourne'),
    ('Australia/Brisbane',      'Australia — Brisbane'),
    ('Australia/Adelaide',      'Australia — Adelaide'),
    ('Australia/Perth',         'Australia — Perth'),
    ('Australia/Darwin',        'Australia — Darwin'),
    ('Pacific/Auckland',        'New Zealand — Auckland'),
    ('Pacific/Fiji',            'Fiji'),
    ('Pacific/Honolulu',        'Hawaii (Pacific)'),
    ('Pacific/Guam',            'Guam'),
    ('Pacific/Tahiti',          'French Polynesia — Tahiti'),
    ('Pacific/Samoa',           'Samoa'),
    # ── UTC offsets ──
    ('UTC',                     'UTC'),
    ('Etc/GMT+12',              'UTC-12'),
    ('Etc/GMT+11',              'UTC-11'),
    ('Etc/GMT+10',              'UTC-10'),
    ('Etc/GMT+9',               'UTC-9'),
    ('Etc/GMT+8',               'UTC-8'),
    ('Etc/GMT+7',               'UTC-7'),
    ('Etc/GMT+6',               'UTC-6'),
    ('Etc/GMT+5',               'UTC-5'),
    ('Etc/GMT+4',               'UTC-4'),
    ('Etc/GMT+3',               'UTC-3'),
    ('Etc/GMT+2',               'UTC-2'),
    ('Etc/GMT+1',               'UTC-1'),
    ('Etc/GMT-1',               'UTC+1'),
    ('Etc/GMT-2',               'UTC+2'),
    ('Etc/GMT-3',               'UTC+3'),
    ('Etc/GMT-4',               'UTC+4'),
    ('Etc/GMT-5',               'UTC+5'),
    ('Etc/GMT-6',               'UTC+6'),
    ('Etc/GMT-7',               'UTC+7'),
    ('Etc/GMT-8',               'UTC+8'),
    ('Etc/GMT-9',               'UTC+9'),
    ('Etc/GMT-10',              'UTC+10'),
    ('Etc/GMT-11',              'UTC+11'),
    ('Etc/GMT-12',              'UTC+12'),
    ('Etc/GMT-13',              'UTC+13'),
    ('Etc/GMT-14',              'UTC+14'),
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

def format_phone(phone):
    digits = ''.join(filter(str.isdigit, phone))
    if len(digits) == 10:
        return f'+1{digits}'
    if len(digits) == 11 and digits[0] == '1':
        return f'+{digits}'
    return f'+{digits}'

def send_via_twilio(acfg, to_phone, text_body):
    from twilio.rest import Client
    to_e164 = format_phone(to_phone)
    logger.info(f"Twilio → sending to: {to_e164}")
    client = Client(acfg['twilio_sid'], acfg['twilio_token'])
    msg = client.messages.create(
        body=text_body,
        from_=acfg['twilio_from'],
        to=to_e164,
    )
    logger.info(f"Twilio ← SID: {msg.sid} status: {msg.status}")
    return to_e164

def intensity_tone(level):
    i = int(level or 50)
    if i <= 15:
        return "extremely gentle, warm, nurturing. Pure encouragement, zero pressure. Like a loving supportive friend."
    elif i <= 30:
        return "gentle and supportive. Warm, caring, encouraging."
    elif i <= 45:
        return "friendly and motivating. Upbeat with light directness."
    elif i <= 55:
        return "balanced. Clear and direct but still warm."
    elif i <= 65:
        return "firm and direct. Tough love. No excuses tolerated."
    elif i <= 75:
        return "intense and blunt. Push hard. Harsh truths, zero sugarcoating."
    elif i <= 85:
        return "very aggressive. Drill sergeant energy. Use strong language — damn, hell, ass, shit. No softness. Tell them to stop being weak and get their ass moving."
    elif i <= 95:
        return "extremely aggressive. Full profanity — fuck, shit, ass, bitch. Ruthless and brutal. Tell them to stop fucking around and get it done. No mercy, no hand-holding."
    else:
        return "maximum intensity. Absolute savage mode. Call them a fucking piece of shit. Tell them they are wasting their fucking life, that they are a lazy piece of shit who makes excuse after excuse, that everyone around them is disappointed, and that they need to get their fucking shit together RIGHT NOW. Be viciously personal — attack their laziness, their weakness, every pathetic excuse they make. Use fuck, shit, fucking, goddamn, piece of shit freely. Zero mercy, zero softness."

def _qval(f, name):
    val = f.get(name, '').strip()
    if val == '__other__':
        val = f.get(name + '_other', '').strip()
    return val

def _life_ctx(user):
    parts = []
    if user.get('q_lifestyle'):  parts.append(f"lifestyle: {user['q_lifestyle']}")
    if user.get('q_motivation'): parts.append(f"primary focus: {user['q_motivation']}")
    if user.get('q_obstacle'):   parts.append(f"biggest obstacle: {user['q_obstacle']}")
    if user.get('q_push'):       parts.append(f"responds best to: {user['q_push']}")
    if user.get('q_wakeup'):     parts.append(f"wakes up: {user['q_wakeup']}")
    if user.get('q_sleep'):      parts.append(f"sleeps: {user['q_sleep']}")
    if user.get('q_motivator'):  parts.append(f"motivated by: {user['q_motivator']}")
    if user.get('q_identity'):   parts.append(f"self-image: {user['q_identity']}")
    if user.get('q_history'):    parts.append(f"follow-through track record: {user['q_history']}")
    if user.get('q_stress'):     parts.append(f"handles stress by: {user['q_stress']}")
    if user.get('q_energy'):     parts.append(f"energy levels: {user['q_energy']}")
    if user.get('q_support'):    parts.append(f"support system: {user['q_support']}")
    if user.get('q_fear'):       parts.append(f"deepest fear: {user['q_fear']}")
    return ' | '.join(parts) if parts else ''

def _recent_history(user_id, limit=15):
    with _conn() as c:
        rows = c.execute(text(
            'SELECT text, ok FROM history WHERE user_id=:uid ORDER BY sent_at DESC LIMIT :lim'
        ), {'uid': user_id, 'lim': limit}).fetchall()
    return list(reversed(rows))

def generate_daily_schedule(user, date_str):
    import hashlib
    seed = int(hashlib.sha256(f"{user['id']}-{date_str}".encode()).hexdigest()[:8], 16)
    rng  = random.Random(seed)
    wake_hour = {
        'Before 6am': 5, '6am to 8am': 7,
        '8am to 10am': 9, 'After 10am': 10,
    }.get(user.get('q_wakeup') or '6am to 8am', 7)
    lifestyle = user.get('q_lifestyle') or 'Working professional'
    if lifestyle == 'Working professional':
        windows = [(wake_hour, wake_hour+1), (12, 13), (18, 20)]
    elif lifestyle == 'Student':
        windows = [(wake_hour, wake_hour+1), (14, 16), (20, 22)]
    elif lifestyle == 'Entrepreneur':
        windows = [(wake_hour, wake_hour+1), (11, 13), (17, 19)]
    else:
        windows = [(wake_hour+1, wake_hour+2), (13, 15), (17, 19)]
    times = []
    for lo, hi in windows:
        h = rng.randint(lo, max(lo, hi - 1))
        m = rng.randint(0, 59)
        times.append(f"{h:02d}:{m:02d}")
    return times

def generate_message(user, acfg):
    client  = anthropic.Anthropic(api_key=acfg['claude_key'])
    hour    = datetime.now().hour
    tod     = 'morning' if hour < 12 else 'afternoon' if hour < 17 else 'evening'
    tone    = intensity_tone(user.get('intensity', 50))
    life    = _life_ctx(user)
    hist    = _recent_history(user['id'])
    history_lines = '\n'.join(
        f"{'Coach' if r[1] else 'Error'}: {r[0]}" for r in hist
    ) if hist else 'None yet'
    prompt = (
        f"You are NexusCoach, personally coaching {user['name']} via SMS.\n"
        f"Their goal: {user['goal']}\n"
        f"About them: {life}\n"
        f"Tone: {tone}\n"
        f"Time of day: {tod}\n\n"
        f"Your message history with them:\n{history_lines}\n\n"
        "Write a new motivational SMS under 155 characters. "
        "Never repeat something you've already sent. "
        "Build on the conversation — reference their goal specifically. "
        "No hashtags, no quotes, just the text."
    )
    resp = client.messages.create(
        model='claude-sonnet-4-6', max_tokens=100,
        messages=[{'role': 'user', 'content': prompt}]
    )
    return resp.content[0].text.strip().strip('"\'')

def send_welcome_sms(user, acfg):
    if not acfg.get('claude_key') or not acfg.get('twilio_sid'):
        return
    tone      = intensity_tone(user.get('intensity', 50))
    life_parts = []
    if user.get('q_lifestyle'):   life_parts.append(user['q_lifestyle'])
    if user.get('q_motivation'):  life_parts.append(f"focused on {user['q_motivation'].lower()}")
    if user.get('q_obstacle'):    life_parts.append(f"struggles with {user['q_obstacle'].lower()}")
    life_ctx = ', '.join(life_parts) if life_parts else ''
    prompt = (
        f"You are NexusCoach texting {user['name']} for the very first time.\n"
        f"Their goal: {user['goal']}\n"
        f"About them: {life_ctx}\n"
        f"Tone: {tone}\n\n"
        "Skip any welcome or greeting. Jump straight into coaching — ask them one sharp, "
        "personal question about their goal or what's been holding them back. "
        "Under 155 characters. No hashtags, no quotes, just the message text."
    )
    try:
        client = anthropic.Anthropic(api_key=acfg['claude_key'])
        resp   = client.messages.create(
            model='claude-sonnet-4-6', max_tokens=100,
            messages=[{'role': 'user', 'content': prompt}]
        )
        msg = resp.content[0].text.strip().strip('"\'')
        send_via_twilio(acfg, user['phone'], msg)
        with _conn() as c:
            c.execute(text('INSERT INTO history (user_id,text,ok) VALUES (:u,:t,1)'),
                      {'u': user['id'], 't': msg})
            c.commit()
    except Exception as e:
        logger.error(f"Welcome SMS error: {e}")

def do_send_user(user, acfg):
    try:
        today      = datetime.now().strftime('%Y-%m-%d')
        limit      = acfg['daily_limit']
        msgs_today = user['msgs_today'] if user.get('msgs_date') == today else 0
        if msgs_today >= limit:
            return False, f'Daily limit ({limit}) reached'

        msg  = generate_message(user, acfg)
        addr = send_via_twilio(acfg, user['phone'], msg)

        with _conn() as c:
            c.execute(text('UPDATE users SET msgs_today=:n, msgs_date=:d WHERE id=:id'),
                      {'n': msgs_today + 1, 'd': today, 'id': user['id']})
            c.execute(text('INSERT INTO history (user_id,text,ok) VALUES (:u,:t,1)'),
                      {'u': user['id'], 't': msg})
            c.commit()

        logger.info(f"Sent → {user['name']} ({addr}): {msg[:60]}")
        return True, msg, addr

    except Exception as e:
        with _conn() as c:
            c.execute(text('INSERT INTO history (user_id,text,ok) VALUES (:u,:t,0)'),
                      {'u': user['id'], 't': str(e)})
            c.commit()
        logger.error(f"Failed → {user.get('email')}: {e}")
        return False, str(e), None

def check_and_send():
    acfg = get_admin_cfg()
    if acfg['paused'] or not acfg['claude_key'] or not acfg['twilio_sid']:
        return
    with _conn() as c:
        rows = c.execute(text('SELECT * FROM users WHERE is_active=1 AND setup_done=1 AND is_admin=0')).mappings().fetchall()
    users = [dict(r) for r in rows]
    for user in users:
        try:
            tz = pytz.timezone(user.get('tz') or 'US/Eastern')
        except Exception:
            tz = pytz.timezone('US/Eastern')
        now_local = datetime.now(tz)
        cur_min   = now_local.strftime('%H:%M')
        today     = now_local.strftime('%Y-%m-%d')
        schedule  = generate_daily_schedule(user, today)
        if cur_min not in schedule:
            continue
        do_send_user(user, acfg)

# ── Static ────────────────────────────────────────────────────────────────────

@app.route('/icons/<path:filename>')
def icons(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'icons'), filename)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/debug')
def debug_info():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    user_count = db.execute(text('SELECT COUNT(*) FROM users')).fetchone()[0]
    return jsonify({
        'database': 'postgresql' if IS_PG else 'sqlite',
        'db_url_prefix': _raw_url[:30] + '...',
        'secret_key_prefix': app.secret_key[:8] + '...',
        'user_count': user_count,
        'session_user_id': session.get('user_id'),
    })


MEDIA_DEFLECTS = [
    "I'm a coach, not a camera. Send me an update on your progress instead.",
    "Can't open that. But I can open a conversation about why you haven't hit your goal yet.",
    "I'm text-only. Save the media for Instagram — send me a real update.",
    "No visuals here. Just results. How's the goal going?",
    "I don't do attachments. Only accountability. What's actually going on today?",
]

@app.route('/webhook/sms', methods=['POST'])
def sms_webhook():
    from_number = request.form.get('From', '')
    body        = request.form.get('Body', '').strip()
    num_media   = int(request.form.get('NumMedia', 0))
    logger.info(f"Incoming SMS from {from_number}: {body!r} (media={num_media})")

    digits = ''.join(filter(str.isdigit, from_number))[-10:]
    with _conn() as c:
        row = c.execute(text(
            'SELECT * FROM users WHERE phone LIKE :d AND is_admin=0'
        ), {'d': f'%{digits}'}).mappings().fetchone()
        user = dict(row) if row else None

    def twiml(msg):
        return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{msg}</Message></Response>', 200, {'Content-Type': 'text/xml'}

    if not user:
        return twiml("Hey! I don't recognize this number. Sign up at nexuscoach.app to get started.")

    if not user.get('is_active'):
        return twiml("Your account is pending activation. You'll get a message from us as soon as you're approved — hang tight!")

    # Media messages (images, video, voice, etc.) — deflect creatively
    if num_media > 0 or not body:
        reply = random.choice(MEDIA_DEFLECTS)
        with _conn() as c:
            c.execute(text('INSERT INTO history (user_id,text,ok) VALUES (:u,:t,1)'),
                      {'u': user['id'], 't': f'[You] [sent media]'})
            c.execute(text('INSERT INTO history (user_id,text,ok) VALUES (:u,:t,1)'),
                      {'u': user['id'], 't': f'[Reply] {reply}'})
            c.commit()
        return twiml(reply)

    acfg = get_admin_cfg()
    if not acfg['claude_key']:
        return twiml("Hey! Something's off on our end — try again soon.")

    # Save the user's inbound message before generating the reply
    with _conn() as c:
        c.execute(text('INSERT INTO history (user_id,text,ok) VALUES (:u,:t,1)'),
                  {'u': user['id'], 't': f'[You] {body}'})
        c.commit()

    hist = _recent_history(user['id'], limit=20)
    history_lines = '\n'.join(
        f"{'Coach' if r[1] else 'System'}: {r[0]}" for r in hist
    ) if hist else 'No prior messages'

    tone = intensity_tone(user.get('intensity', 50))
    life = _life_ctx(user)
    prompt = (
        f"You are NexusCoach, personally coaching {user['name']} via SMS.\n"
        f"Their goal: {user['goal']}\n"
        f"About them: {life}\n"
        f"Tone: {tone}\n\n"
        f"Full conversation history:\n{history_lines}\n\n"
        f"They just sent: \"{body}\"\n\n"
        "IMPORTANT RULES:\n"
        "- If what they sent is completely off-topic (weather, news, jokes, trivia, random questions), "
        "do NOT answer it. Instead respond with a short, witty, creative deflection that ties back to "
        "their goal. Be clever — not robotic. Example: if they ask the weather, say something like "
        "'Only forecast I care about: you crushing your goal today.'\n"
        "- Never repeat something from the history above.\n"
        "- Otherwise: acknowledge what they said and keep coaching them toward their goal.\n"
        "Under 155 characters. No hashtags, no quotes, just the text."
    )

    try:
        client = anthropic.Anthropic(api_key=acfg['claude_key'])
        resp   = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=80,
            messages=[{'role': 'user', 'content': prompt}]
        )
        reply = resp.content[0].text.strip().strip('"\'')
        with _conn() as c:
            c.execute(text('INSERT INTO history (user_id,text,ok) VALUES (:u,:t,1)'),
                      {'u': user['id'], 't': f'[Reply] {reply}'})
            c.commit()
    except Exception as e:
        logger.error(f"Webhook reply error: {e}")
        reply = "Got your message! Keep pushing — you're doing great."

    return twiml(reply)

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
            if db.execute(text('SELECT id FROM users WHERE email=:e AND is_admin=0'), {'e': email}).fetchone():
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

@app.route('/verify', methods=['GET', 'POST'])
@login_required
def verify():
    db   = get_db()
    user = db.execute(text('SELECT * FROM users WHERE id=:id'),
                      {'id': session['user_id']}).mappings().fetchone()
    if not user:
        session.clear()
        return redirect(url_for('login'))
    if user['phone_verified']:
        return redirect(url_for('dashboard'))
    error = None
    if request.method == 'POST':
        entered = request.form.get('code', '').strip()
        if entered == str(user['verify_code']):
            db.execute(text('UPDATE users SET phone_verified=1 WHERE id=:id'),
                       {'id': session['user_id']})
            db.commit()
            fresh = db.execute(text('SELECT * FROM users WHERE id=:id'),
                               {'id': session['user_id']}).mappings().fetchone()
            fresh = db.execute(text('SELECT * FROM users WHERE id=:id'),
                               {'id': session['user_id']}).mappings().fetchone()
            if fresh:
                acfg = get_admin_cfg()
                send_welcome_sms(dict(fresh), acfg)
            return redirect(url_for('dashboard'))
        else:
            error = 'Incorrect code. Check your texts and try again.'
    return render_template('verify.html', phone=user['phone'], error=error)

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
            if row['phone'] and not row['phone_verified']:
                return redirect(url_for('verify'))
            if row['is_admin']:
                return redirect(url_for('admin'))
            return redirect(url_for('dashboard'))
    return render_template('login.html', error=error)

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    error = None
    sent  = False
    if request.method == 'POST':
        phone_raw = request.form.get('phone', '').strip()
        digits    = ''.join(filter(str.isdigit, phone_raw))[-10:]
        if len(digits) < 10:
            error = 'Enter a valid 10-digit phone number.'
        else:
            db  = get_db()
            row = db.execute(text(
                "SELECT * FROM users WHERE phone LIKE :d AND is_admin=0"
            ), {'d': f'%{digits}'}).mappings().fetchone()
            if not row:
                error = 'No account found with that phone number.'
            else:
                code = str(random.randint(100000, 999999))
                db.execute(text('UPDATE users SET reset_code=:c WHERE id=:id'),
                           {'c': code, 'id': row['id']})
                db.commit()
                acfg = get_admin_cfg()
                try:
                    twilio_client = Client(acfg['twilio_sid'], acfg['twilio_token'])
                    twilio_client.messages.create(
                        to=row['phone'],
                        from_=acfg['twilio_from'],
                        body=f"NexusCoach password reset code: {code}. Valid for 10 minutes."
                    )
                    session['reset_uid'] = row['id']
                    return redirect(url_for('reset_password'))
                except Exception as e:
                    error = f'Could not send SMS: {e}'
    return render_template('forgot_password.html', error=error)

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    uid = session.get('reset_uid')
    if not uid:
        return redirect(url_for('forgot_password'))
    error = None
    if request.method == 'POST':
        code     = request.form.get('code', '').strip()
        new_pw   = request.form.get('password', '').strip()
        confirm  = request.form.get('confirm', '').strip()
        db  = get_db()
        row = db.execute(text('SELECT * FROM users WHERE id=:id'), {'id': uid}).mappings().fetchone()
        if not row:
            return redirect(url_for('forgot_password'))
        if not code or code != str(row['reset_code']):
            error = 'Incorrect code. Check your texts and try again.'
        elif len(new_pw) < 6:
            error = 'Password must be at least 6 characters.'
        elif new_pw != confirm:
            error = 'Passwords do not match.'
        else:
            db.execute(text('UPDATE users SET password_hash=:h, reset_code=:rc WHERE id=:id'),
                       {'h': generate_password_hash(new_pw), 'rc': '', 'id': uid})
            db.commit()
            session.pop('reset_uid', None)
            return redirect(url_for('login'))
    return render_template('reset_password.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('landing'))

@app.route('/setup', methods=['GET', 'POST'])
@login_required
def setup():
    import random
    db   = get_db()
    user = db.execute(text('SELECT * FROM users WHERE id=:id'),
                      {'id': session['user_id']}).mappings().fetchone()
    user = dict(user) if user else {}
    error = None
    if request.method == 'POST':
        f         = request.form
        times     = [t.strip() for t in f.getlist('times') if t.strip()]
        raw_phone  = f.get('phone','').strip()
        new_phone  = format_phone(raw_phone) if raw_phone else ''
        new_digits = ''.join(filter(str.isdigit, raw_phone))[-10:] if raw_phone else ''

        # Duplicate phone check (compare by last 10 digits to handle format differences)
        if new_digits:
            dupe = db.execute(text(
                "SELECT id FROM users WHERE phone LIKE :p AND id!=:id AND is_admin=0"
            ), {'p': f'%{new_digits}', 'id': session['user_id']}).fetchone()
            if dupe:
                error = 'That phone number is already registered to another account.'
                return render_template('setup.html', user=user, timezones=TIMEZONES, error=error)

        old_digits    = ''.join(filter(str.isdigit, user.get('phone') or ''))[-10:]
        phone_changed = new_digits != old_digits
        was_verified  = bool(user.get('phone_verified'))
        needs_verify  = new_phone and (phone_changed or not was_verified)

        db.execute(text("""
            UPDATE users SET phone=:ph, goal=:go, intensity=:iv,
                             q_wakeup=:qw, q_motivation=:qm, q_obstacle=:qo,
                             q_lifestyle=:ql, q_push=:qp,
                             q_motivator=:qmot, q_identity=:qid, q_history=:qhist,
                             q_stress=:qstr, q_energy=:qen, q_sleep=:qslp,
                             q_support=:qsup, q_fear=:qfear,
                             times=:ti, freq=:fr, tz=:tz, setup_done=1,
                             phone_verified=:pv
            WHERE id=:id
        """), {
            'ph': raw_phone, 'go': f.get('goal','').strip(),
            'iv': int(f.get('intensity', 50)),
            'qw': _qval(f,'q_wakeup'),    'qm': _qval(f,'q_motivation'),
            'qo': _qval(f,'q_obstacle'),  'ql': _qval(f,'q_lifestyle'),
            'qp': _qval(f,'q_push'),
            'qmot':  _qval(f,'q_motivator'), 'qid':  _qval(f,'q_identity'),
            'qhist': _qval(f,'q_history'),   'qstr': _qval(f,'q_stress'),
            'qen':   _qval(f,'q_energy'),    'qslp': _qval(f,'q_sleep'),
            'qsup':  _qval(f,'q_support'),   'qfear': _qval(f,'q_fear'),
            'ti': json.dumps(times or ['08:00']), 'fr': f.get('freq','daily'),
            'tz': f.get('tz','US/Eastern'),
            'pv': 0 if needs_verify else (1 if was_verified else 0),
            'id': session['user_id'],
        })
        db.commit()

        if needs_verify:
            acfg = get_admin_cfg()
            if acfg['twilio_sid'] and acfg['twilio_token'] and acfg['twilio_from']:
                code = str(random.randint(100000, 999999))
                db.execute(text('UPDATE users SET verify_code=:c WHERE id=:id'),
                           {'c': code, 'id': session['user_id']})
                db.commit()
                try:
                    send_via_twilio(acfg, new_phone, f'Your NexusCoach code: {code}')
                except Exception as ex:
                    logger.warning(f"SMS verify send failed: {ex}")
                return redirect(url_for('verify'))
            else:
                # Twilio not configured — skip verification
                db.execute(text('UPDATE users SET phone_verified=1 WHERE id=:id'),
                           {'id': session['user_id']})
                db.commit()

        return redirect(url_for('dashboard'))
    return render_template('setup.html', user=user, timezones=TIMEZONES, error=error)

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
    if user['is_admin']:
        return redirect(url_for('admin'))
    hist_rows = db.execute(text("""
        SELECT text, ok, sent_at FROM history
        WHERE user_id=:uid ORDER BY sent_at DESC LIMIT 20
    """), {'uid': user['id']}).fetchall()
    hist  = [{'text': r[0], 'ok': r[1], 'sent_fmt': fmt_time(r[2])} for r in hist_rows]
    acfg  = get_admin_cfg()
    ready = bool(acfg['claude_key'] and acfg['twilio_sid'] and acfg['twilio_token'] and acfg['twilio_from'])
    today = datetime.now().strftime('%Y-%m-%d')
    msgs_today = user.get('msgs_today', 0) if user.get('msgs_date') == today else 0
    times = json.loads(user.get('times') or '["08:00"]')
    return render_template('dashboard.html',
        user=user, hist=hist, ready=ready, times=times,
        msgs_today=msgs_today, limit=acfg['daily_limit'])

@app.route('/test', methods=['POST'])
@login_required
def test_message():
    db   = get_db()
    user = db.execute(text('SELECT * FROM users WHERE id=:id'),
                      {'id': session['user_id']}).mappings().fetchone()
    user = dict(user) if user else {}
    if user.get('is_admin'):
        return jsonify({'ok': False, 'error': 'Admin accounts do not receive messages.'})
    if not user.get('setup_done'):
        return jsonify({'ok': False, 'error': 'Complete setup first'})
    acfg = get_admin_cfg()
    if not acfg['claude_key'] or not acfg['twilio_sid']:
        return jsonify({'ok': False, 'error': 'Admin config not complete yet'})
    ok, txt, addr = do_send_user(user, acfg)
    return jsonify({'ok': ok, 'message': txt if ok else None, 'error': txt if not ok else None, 'addr': addr})

@app.route('/admin/conversations')
@admin_required
def admin_conversations():
    db    = get_db()
    users = db.execute(text("""
        SELECT u.id, u.name, u.phone, u.email,
               COUNT(h.id) AS msg_count,
               MAX(h.sent_at) AS last_msg
        FROM users u
        LEFT JOIN history h ON h.user_id = u.id
        GROUP BY u.id
        ORDER BY MAX(h.sent_at) DESC
    """)).mappings().fetchall()
    return render_template('conversations.html', users=[dict(u) for u in users])

@app.route('/admin/users/<int:uid>/set-password', methods=['POST'])
@admin_required
def admin_set_password(uid):
    data   = request.get_json(silent=True) or {}
    new_pw = data.get('password', '').strip()
    if len(new_pw) < 6:
        return jsonify({'ok': False, 'error': 'Password must be at least 6 characters.'})
    db = get_db()
    db.execute(text('UPDATE users SET password_hash=:h WHERE id=:id'),
               {'h': generate_password_hash(new_pw), 'id': uid})
    db.commit()
    return jsonify({'ok': True})

@app.route('/admin/conversations/<int:uid>/messages')
@admin_required
def conversation_messages(uid):
    db   = get_db()
    user = db.execute(text('SELECT * FROM users WHERE id=:id'),
                      {'id': uid}).mappings().fetchone()
    if not user:
        return jsonify({'error': 'not found'}), 404
    msgs = db.execute(text("""
        SELECT id, text, ok, sent_at FROM history
        WHERE user_id=:uid ORDER BY sent_at ASC
    """), {'uid': uid}).mappings().fetchall()
    u = dict(user)
    return jsonify({
        'user': {
            'id':       u['id'],
            'name':     u['name'],
            'email':    u['email'],
            'phone':    u['phone'],
            'created':  u.get('created_at', ''),
            'active':   bool(u.get('is_active')),
            'verified': bool(u.get('phone_verified')),
            'goal':     u.get('goal', ''),
            'pw_hash':  u.get('password_hash', ''),
        },
        'messages': [dict(m) for m in msgs],
    })

@app.route('/admin')
@admin_required
def admin():
    acfg  = get_admin_cfg()
    db    = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    users = db.execute(text("""
        SELECT id, name, email, phone, is_admin, is_active, setup_done,
               msgs_today, msgs_date, created_at, goal, tz, intensity,
               q_lifestyle, q_motivation, q_obstacle, phone_verified
        FROM users ORDER BY created_at DESC
    """)).mappings().fetchall()
    users = [dict(u) for u in users]
    for u in users:
        u['joined'] = fmt_time(u.get('created_at')) or '—'
    # stats
    total_users   = len(users)
    active_users  = sum(1 for u in users if u['is_active'] and not u['is_admin'])
    msgs_today    = db.execute(text(
        "SELECT COALESCE(SUM(msgs_today),0) FROM users WHERE msgs_date=:d AND is_admin=0"
    ), {'d': today}).scalar() or 0
    msgs_all_time = db.execute(text("SELECT COUNT(*) FROM history")).scalar() or 0
    # is this the original (super) admin?
    orig = db.execute(text("SELECT id FROM users WHERE is_admin=1 ORDER BY id ASC LIMIT 1")).fetchone()
    is_super = orig and orig[0] == session['user_id']
    return render_template('admin.html', cfg=acfg, users=users, today=today,
                           total_users=total_users, active_users=active_users,
                           msgs_today=msgs_today, msgs_all_time=msgs_all_time,
                           is_super=is_super)

@app.route('/admin/config', methods=['POST'])
@admin_required
def admin_config():
    f  = request.form
    db = get_db()
    for key in ('claude_key', 'twilio_sid', 'twilio_token', 'twilio_from'):
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
    user = db.execute(text('SELECT * FROM users WHERE id=:id'), {'id': uid}).mappings().fetchone()
    if user:
        acfg = get_admin_cfg()
        send_welcome_sms(dict(user), acfg)
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

@app.route('/admin/users/<int:uid>/send-message', methods=['POST'])
@admin_required
def admin_send_message(uid):
    db   = get_db()
    user = db.execute(text('SELECT * FROM users WHERE id=:id AND is_admin=0'),
                      {'id': uid}).mappings().fetchone()
    if not user:
        return jsonify({'ok': False, 'error': 'User not found'})
    acfg = get_admin_cfg()
    if not acfg['claude_key'] or not acfg['twilio_sid']:
        return jsonify({'ok': False, 'error': 'Admin config incomplete'})
    ok, txt, addr = do_send_user(dict(user), acfg)
    return jsonify({'ok': ok, 'message': txt if ok else None, 'error': txt if not ok else None})

@app.route('/admin/users/<int:uid>/clear-history', methods=['POST'])
@admin_required
def admin_clear_history(uid):
    db = get_db()
    db.execute(text('DELETE FROM history WHERE user_id=:id'), {'id': uid})
    db.commit()
    return jsonify({'ok': True})

@app.route('/admin/grant-admin', methods=['GET', 'POST'])
@admin_required
def grant_admin():
    # Only the original (lowest-id) admin can access this
    db   = get_db()
    orig = db.execute(text("SELECT id FROM users WHERE is_admin=1 ORDER BY id ASC LIMIT 1")).fetchone()
    if not orig or orig[0] != session['user_id']:
        return redirect(url_for('admin'))
    step  = request.form.get('step', '1') if request.method == 'POST' else '1'
    uid   = request.form.get('uid', '')
    error = None
    non_admins = db.execute(text(
        "SELECT id, name, email FROM users WHERE is_admin=0 ORDER BY name ASC"
    )).mappings().fetchall()
    non_admins = [dict(u) for u in non_admins]
    if request.method == 'POST':
        if step == '1':
            if not uid:
                error = 'Select a user.'
            else:
                target = db.execute(text('SELECT * FROM users WHERE id=:id AND is_admin=0'),
                                    {'id': uid}).mappings().fetchone()
                if not target:
                    error = 'User not found.'
                else:
                    return render_template('grant_admin.html', step='2', uid=uid,
                                           target=dict(target), non_admins=non_admins, error=None)
        elif step == '2':
            confirm = request.form.get('confirm', '')
            if confirm != 'GRANT ADMIN':
                error = 'Type exactly: GRANT ADMIN'
            else:
                target = db.execute(text('SELECT * FROM users WHERE id=:id AND is_admin=0'),
                                    {'id': uid}).mappings().fetchone()
                return render_template('grant_admin.html', step='3', uid=uid,
                                       target=dict(target) if target else {}, non_admins=non_admins, error=None)
        elif step == '3':
            me = db.execute(text('SELECT * FROM users WHERE id=:id'),
                            {'id': session['user_id']}).mappings().fetchone()
            if not me or not check_password_hash(me['password_hash'], request.form.get('password', '')):
                error = 'Incorrect password.'
                target = db.execute(text('SELECT * FROM users WHERE id=:id'),
                                    {'id': uid}).mappings().fetchone()
                return render_template('grant_admin.html', step='3', uid=uid,
                                       target=dict(target) if target else {}, non_admins=non_admins, error=error)
            db.execute(text('UPDATE users SET is_admin=1, is_active=0 WHERE id=:id'), {'id': uid})
            db.commit()
            return redirect(url_for('admin'))
    return render_template('grant_admin.html', step='1', uid='', target={},
                           non_admins=non_admins, error=error)

@app.route('/account/delete', methods=['GET', 'POST'])
@login_required
def account_delete():
    db   = get_db()
    user = db.execute(text('SELECT * FROM users WHERE id=:id'),
                      {'id': session['user_id']}).mappings().fetchone()
    if not user:
        session.clear()
        return redirect(url_for('login'))
    if user['is_admin']:
        return redirect(url_for('dashboard'))
    step  = request.form.get('step', '1') if request.method == 'POST' else '1'
    error = None

    if request.method == 'POST':
        if step == '1':
            # Step 2: must type DELETE
            return render_template('account_delete.html', step='2', user=dict(user), error=None)
        elif step == '2':
            word = request.form.get('confirm_word', '').strip()
            if word != 'DELETE':
                error = 'You must type DELETE exactly.'
                return render_template('account_delete.html', step='2', user=dict(user), error=error)
            return render_template('account_delete.html', step='3', user=dict(user), error=None)
        elif step == '3':
            password = request.form.get('password', '').strip()
            if not check_password_hash(user['password_hash'], password):
                error = 'Incorrect password.'
                return render_template('account_delete.html', step='3', user=dict(user), error=error)
            db.execute(text('DELETE FROM history WHERE user_id=:id'), {'id': user['id']})
            db.execute(text('DELETE FROM users WHERE id=:id'),        {'id': user['id']})
            db.commit()
            session.clear()
            return redirect(url_for('landing'))

    return render_template('account_delete.html', step='1', user=dict(user), error=None)

# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logger.info(f"Database: {'PostgreSQL' if IS_PG else 'SQLite'} ({_raw_url[:40]}...)")
    logger.info(f"Secret key source: {'env var' if os.environ.get('SECRET_KEY') else 'file/generated'}")
    init_db()
    migrate_db()
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
