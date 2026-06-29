#!/usr/bin/env python3
"""NexusCoach Setup Wizard — configure everything in under 2 minutes."""

import sqlite3, smtplib, webbrowser, time, os, sys, getpass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'nexuscoach.db')

def divider():
    print('\n' + '─' * 52)

def save(settings):
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT DEFAULT '')""")
        for k, v in settings.items():
            db.execute('INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)', (k, v))
        db.commit()

def test_gmail(addr, pw):
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10) as s:
            s.login(addr, pw)
        return True, None
    except Exception as e:
        return False, str(e)

def main():
    print('\n  NexusCoach Setup Wizard')
    print('  Configure everything once, done forever.\n')

    # ── Step 1: Gmail App Password ─────────────────────────────────────────────
    divider()
    print('  STEP 1 of 2 — Gmail App Password')
    divider()
    print("""
  Opening Google App Passwords in your browser...
  (Log in with the Gmail you want NexusCoach to send from)

  Once logged in:
    1. Type "NexusCoach" in the app name box
    2. Click Create
    3. Copy the 16-character code
    4. Come back here
""")
    webbrowser.open('https://myaccount.google.com/apppasswords')
    time.sleep(2)

    gmail = input('  Gmail address: ').strip()

    while True:
        app_pass = getpass.getpass('  App Password (hidden as you type): ').strip().replace(' ', '')
        print('  Testing connection...', end='', flush=True)
        ok, err = test_gmail(gmail, app_pass)
        if ok:
            print(' ✓ Works!')
            break
        else:
            print(f' ✗  Failed — {err}')
            retry = input('  Try again? (y/n): ').strip().lower()
            if retry != 'y':
                print('\n  Check that 2-Step Verification is on and the App Password is correct.')
                sys.exit(1)

    # ── Step 2: Claude API Key ─────────────────────────────────────────────────
    divider()
    print('  STEP 2 of 2 — Claude API Key')
    divider()
    print("""
  Opening Anthropic console in your browser...
  Go to API Keys → Create Key → copy it
""")
    webbrowser.open('https://console.anthropic.com/settings/keys')
    time.sleep(1.5)

    claude_key = getpass.getpass('  Claude API Key (hidden as you type): ').strip()

    # ── Save ───────────────────────────────────────────────────────────────────
    save({
        'claude_key':  claude_key,
        'gmail':       gmail,
        'gmail_pass':  app_pass,
        'daily_limit': '3',
        'paused':      '0',
    })

    divider()
    print("""
  ✅  Done! Everything is configured.

  Next:
    1. Close this window
    2. Double-click run.bat
    3. Sign up at http://localhost:5000
    4. Fill in your phone and goal
    5. Hit "Send Test Now"
""")

if __name__ == '__main__':
    main()
