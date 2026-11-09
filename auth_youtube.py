# -*- coding: utf-8 -*-
"""
إذنٌ لبوت المواقع ليضيف الفيديوهات إلى قوائم تشغيلك في يوتيوب. يُشغَّل مرّةً واحدة على جهازك:

  python auth_youtube.py "مسار ملف client_secret_....json"

يفتح المتصفّح، فتختار حسابك وتسمح. ثمّ يكتب على سطح المكتب الملفّ youtube-secrets.txt فيه ثلاث قيم،
تضعها بيدك أسرارًا في المستودع (Settings ← Secrets and variables ← Actions)، ثمّ تحذف الملفّ.
"""
import base64
import hashlib
import http.server
import json
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path

import requests

SCOPE = 'https://www.googleapis.com/auth/youtube'


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding='utf-8', errors='replace')
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    j = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    c = j.get('installed') or j.get('web')
    cid, sec = c['client_id'], c['client_secret']

    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            msg = 'تمّ. ارجع إلى النافذة السوداء.' if 'code' in got else 'لم يتمّ الإذن. ارجع إلى النافذة السوداء.'
            self.wfile.write(f'<h2 dir="rtl">{msg}</h2>'.encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(('127.0.0.1', 0), H)
    redirect = f'http://127.0.0.1:{srv.server_port}/'
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(16)
    url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urllib.parse.urlencode({
        'client_id': cid, 'redirect_uri': redirect, 'response_type': 'code', 'scope': SCOPE,
        'access_type': 'offline', 'prompt': 'consent', 'state': state,
        'code_challenge': base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode(),
        'code_challenge_method': 'S256'})
    print('يُفتح المتصفّح الآن: اختر حسابك في يوتيوب، ثمّ اسمح.')
    print('(إن ظهر «لم تتحقّق Google من هذا التطبيق» فاضغط «متقدّم» ثمّ «الانتقال إلى ...»: التطبيق تطبيقك.)')
    webbrowser.open(url)
    while 'code' not in got and 'error' not in got:
        srv.handle_request()
    if got.get('state') != state or 'code' not in got:
        sys.exit(f'لم يتمّ الإذن: {got.get("error", "ردٌّ غير متوقّع")}')

    r = requests.post('https://oauth2.googleapis.com/token', timeout=30, data={
        'code': got['code'], 'client_id': cid, 'client_secret': sec, 'redirect_uri': redirect,
        'grant_type': 'authorization_code', 'code_verifier': verifier})
    t = r.json()
    if 'refresh_token' not in t:
        sys.exit(f'لم يعطِ Google إذنًا دائمًا: {t.get("error", r.status_code)}')

    out = Path.home() / 'Desktop' / 'youtube-secrets.txt'
    out.write_text('ضع كلّ قيمةٍ سرًّا بالاسم الذي قبلها، ثمّ احذف هذا الملفّ.\n\n'
                   f'YT_CLIENT_ID\n{cid}\n\nYT_CLIENT_SECRET\n{sec}\n\nYT_REFRESH_TOKEN\n{t["refresh_token"]}\n',
                   encoding='utf-8')
    print(f'تمّ. القيم الثلاث في: {out}')


if __name__ == '__main__':
    main()
