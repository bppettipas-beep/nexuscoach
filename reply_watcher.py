#!/usr/bin/env python3
"""NexusCoach Reply Watcher — watches Gmail for SMS replies and responds with Claude."""

import imaplib
import email
import smtplib
import sqlite3
import time
import os
import logging
from email.mime.text import MIMEText

import anthropic

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'nexuscoach.db')

CARRIERS = {
    'att':        'txt.att.net',
    'tmobile':    'tmomail.net',
    'verizon':    'vtext.com',
    'sprint':     'messaging.sprintpcs.com',
    'metropcs':   'mymetropcs.com',
    'boost':      'sms.myboostmobile.com',
    'cricket':    'sms.cricketwireless.net',
    'uscellular': 'email.uscc.net',
    'googlefi':   'msg.fi.google.com',
}

# Per-user conversation history: {user_id: [{'u':..,'a':..}, ...]}
_histories = {}


def get_setting(key, default=''):
    try:
        with sqlite3.connect(DB_PATH) as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return row[0] if row else default
    except Exception:
        return default

def get_active_users():
    try:
        with sqlite3.connect(DB_PATH) as db:
            db.row_factory = sqlite3.Row
            return db.execute(
                'SELECT * FROM users WHERE is_active=1 AND setup_done=1'
            ).fetchall()
    except Exception as e:
        logger.error(f"DB read failed: {e}")
        return []

def sms_addr(phone, carrier):
    digits = ''.join(filter(str.isdigit, phone))[-10:]
    domain = CARRIERS.get(carrier, 'vtext.com')
    return f"{digits}@{domain}"

def send_sms(gmail, gmail_pass, to_addr, text):
    msg = MIMEText(text)
    msg['From']    = gmail
    msg['To']      = to_addr
    msg['Subject'] = ''
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(gmail, gmail_pass)
        s.sendmail(gmail, to_addr, msg.as_string())

def ai_reply(user, claude_key, user_text):
    client = anthropic.Anthropic(api_key=claude_key)
    uid    = user['id']

    history = _histories.get(uid, [])
    msgs = []
    for ex in history[-6:]:
        msgs.append({'role': 'user',      'content': ex['u']})
        msgs.append({'role': 'assistant', 'content': ex['a']})
    msgs.append({'role': 'user', 'content': user_text})

    system = (
        f"You are NexusCoach, a motivational AI coach texting with {user['name']}. "
        f"Their goal: {user['goal'] or 'living their best life'}. "
        f"Be conversational, warm, and personal. This is SMS — keep every reply under 155 characters. "
        f"No hashtags. No quotes around the message. Match the energy of what they send."
    )

    resp = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=80,
        system=system,
        messages=msgs,
    )
    reply = resp.content[0].text.strip().strip('"\'')
    _histories.setdefault(uid, []).append({'u': user_text, 'a': reply})
    return reply


def check_once():
    claude_key  = get_setting('claude_key')
    gmail       = get_setting('gmail')
    gmail_pass  = get_setting('gmail_pass')

    if not claude_key or not gmail or not gmail_pass:
        logger.warning("Config incomplete — open the admin panel to configure API keys")
        return

    users = get_active_users()
    if not users:
        return

    # Build a map: sms_address → user
    addr_map = {}
    for u in users:
        if u['phone'] and u['carrier']:
            addr_map[sms_addr(u['phone'], u['carrier'])] = dict(u)

    if not addr_map:
        return

    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(gmail, gmail_pass)
        mail.select('INBOX')

        for addr, user in addr_map.items():
            _, ids = mail.search(None, f'UNSEEN FROM "{addr}"')
            msg_ids = ids[0].split()

            if msg_ids:
                logger.info(f"Found {len(msg_ids)} reply(s) from {user['name']}")

            for mid in msg_ids:
                _, data = mail.fetch(mid, '(RFC822)')
                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                body = ''
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == 'text/plain':
                            cs = part.get_content_charset() or 'utf-8'
                            body = part.get_payload(decode=True).decode(cs, errors='replace').strip()
                            break
                else:
                    cs = msg.get_content_charset() or 'utf-8'
                    body = msg.get_payload(decode=True).decode(cs, errors='replace').strip()

                body = body.split('\n\n')[0].strip()[:500]
                if not body:
                    mail.store(mid, '+FLAGS', '\\Seen')
                    continue

                logger.info(f"{user['name']}: {body[:80]}")
                reply = ai_reply(user, claude_key, body)
                send_sms(gmail, gmail_pass, addr, reply)
                logger.info(f"Coach → {user['name']}: {reply}")

                mail.store(mid, '+FLAGS', '\\Seen')

        mail.close()
        mail.logout()

    except Exception as e:
        logger.error(f"Reply check error: {e}")


def main():
    logger.info("NexusCoach Reply Watcher — checking Gmail every 60s")
    logger.info("Keep this window open. Ctrl+C to stop.")
    while True:
        check_once()
        time.sleep(60)


if __name__ == '__main__':
    main()
