# -*- coding: utf-8 -*-
"""
SiteWatch — يفحص المواقع التي تتابعها، ويرسل جديدها إلى بوت تيلغرام واحد.

  python sitewatch.py            فحص وإرسال (يحفظ ما أرسله في state.json)
  python sitewatch.py --dry      فحص بلا إرسال وبلا حفظ: يطبع ما كان سيُرسل
  python sitewatch.py --probe    يجرّب كلّ مصدر (والمعطّلة أيضًا) ويطبع ما وجد، بلا إرسال ولا حفظ
  python sitewatch.py --test     رسالة اختبار فقط

المصادر في config.public.json. والأسرار من متغيّرات البيئة:
  TELEGRAM_TOKEN و TELEGRAM_CHAT_ID  البوت والمحادثة
  YT_API_KEY                         مفتاح YouTube Data API
  YT_SUBSCRIBER                      رابط قناتك: تؤخذ القنوات من اشتراكاتك العامّة، فتتحدّث وحدها
  YT_CHANNELS                        احتياط: معرّفات قنوات (UC...)، إن لم تُقرأ الاشتراكات
  YT_CLIENT_ID و YT_CLIENT_SECRET و YT_REFRESH_TOKEN
                                     إذنك لحسابك في يوتيوب (auth_youtube.py): أزرار «أضف إلى قائمة» تحت كلّ فيديو
  GITHUB_TOKEN                       اختياريّ: يرفع حدّ طلبات واجهة GitHub
"""
import argparse
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / 'config.public.json'
STATE_PATH = ROOT / 'state.json'
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/131.0 Safari/537.36',
      'Accept-Language': 'ar,en;q=0.8'}
KEEP_KEYS = 600          # ما يُحفظ من مفاتيح كلّ مصدر
MAX_SINGLE = 10          # إن زاد جديد مصدرٍ على هذا جُمع في رسالة واحدة
FAIL_ALERT = 3           # تنبيه بعد هذا العدد من الإخفاقات المتتالية
MAX_PLAYLISTS = 8        # أقصى عدد أزرار القوائم تحت الفيديو


class Blocked(Exception):
    """الموقع ردّ بصفحة حماية (Cloudflare ونحوه) لا بمحتواه."""


def get(url, **kw):
    r = requests.get(url, headers={**UA, **kw.pop('headers', {})}, timeout=kw.pop('timeout', 40), **kw)
    if r.status_code in (403, 503) and ('Just a moment' in r.text or 'challenge' in r.text.lower()):
        raise Blocked(f'HTTP {r.status_code}: صفحة حماية')
    r.raise_for_status()
    return r


def clean(t):
    return re.sub(r'\s+', ' ', html.unescape(t or '')).strip()


def utc(t):
    """تاريخ النشر بتوقيت UTC بصيغة ISO، من أيّ صيغةٍ شائعة؛ أو '' إن لم يُعرف."""
    t = clean(t)
    if not t:
        return ''
    try:
        d = datetime.fromisoformat(t.replace('Z', '+00:00').replace(' ', 'T', 1))
    except ValueError:
        try:
            d = parsedate_to_datetime(t)
        except (TypeError, ValueError):
            return ''
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat(timespec='seconds')


# ------------------------------------------------------------------ أنواع المصادر
# كلّ قارئ يعيد قائمة عناصر، الأحدث أوّلًا: {'key', 'title', 'url'}، و'date' (UTC) إن كان للموقع تاريخ نشر.

def rss_page(url):
    root = ET.fromstring(get(url).content)
    out = []
    for it in root.iter('item'):
        link = clean(it.findtext('link'))
        out.append({'key': clean(it.findtext('guid')) or link,
                    'title': clean(it.findtext('title')), 'url': link, 'date': utc(it.findtext('pubDate')),
                    'cats': [clean(c.text).lower() for c in it.findall('category') if c.text]})
    A = '{http://www.w3.org/2005/Atom}'
    for it in root.iter(A + 'entry'):
        a = it.find(A + 'link')
        link = a.get('href') if a is not None else ''
        out.append({'key': clean(it.findtext(A + 'id')) or link, 'title': clean(it.findtext(A + 'title')),
                    'url': link, 'author': clean(it.findtext(f'{A}author/{A}name')),
                    'date': utc(it.findtext(A + 'published') or it.findtext(A + 'updated'))})
    return out


def read_rss(src):
    """خلاصة RSS أو Atom. ومعها اختياريًّا:
    pages_hours: تُقرأ صفحات الخلاصة الأقدم (?paged=2…) حتّى تبلغ ما نُشر قبل هذه الساعات، فلا يضيع ما يُنشر دفعةً
                 واحدة أكثر من صفحة (حتّى max_pages صفحات).
    series_list: صفحةٌ فيها أسماء المسلسلات المطلوبة (مثل قائمة الكرتون في WCO)، فلا يبقى إلّا ما تصنيفه منها."""
    out = rss_page(src['url'])
    if src.get('pages_hours'):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=src['pages_hours'])).isoformat()
        sep = '&' if '?' in src['url'] else '?'
        for n in range(2, src.get('max_pages', 10) + 1):
            dates = [it['date'] for it in out if it.get('date')]
            if not dates or min(dates) < cutoff:
                break
            more = rss_page(f"{src['url']}{sep}paged={n}")
            if not more:
                break
            out += more
    if src.get('series_list'):
        h = get(src['series_list']).text
        want = {clean(t).lower() for t in re.findall(r'<li><a href="/anime/[^"]+" title="([^"]+)"', h)}
        if not want:
            raise ValueError('قائمة المسلسلات فارغة')
        out = [it for it in out if any(c in want for c in it.get('cats', []))]
    seen = set()
    return [seen.add(it['key']) or it for it in out if it['key'] not in seen]


def read_anikoto(src):
    """صفحة الفرز في anikoto: كلّ عملٍ بعدد حلقاته المترجمة؛ والجديد حلقةٌ زادت."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(get(src['url']).text, 'html.parser')
    out = []
    for item in soup.select('#list-items .item'):
        a = item.select_one('a.name')
        sub = item.select_one('.ep-status.sub')
        if not a or not a.get('href'):
            continue
        base = a['href'].rsplit('/ep-', 1)[0]
        ep = clean(sub.get_text()) if sub else ''
        out.append({'key': f'{base}#{ep}', 'title': f'{clean(a.get_text())} — الحلقة {ep}' if ep else clean(a.get_text()),
                    'url': f'{base}/ep-{ep}' if ep.isdigit() else a['href']})
    return out


def read_filecr_pages(src):
    """برامج بعينها في filecr (المثبّتة على حاسوبك): صفحة كلّ برنامج، والمفتاح البرنامج مع تاريخ تحديثه.
    وكلّ عنصرٍ في programs اسمُ صفحته (فهو من ويندوز)، أو قسمها واسمها مثل "android/snaptube".
    والبرنامج المضاف حديثًا يُسجَّل تحديثه الحاليّ بلا إرسال."""
    def data(url, **params):
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', get(url, params=params).text, re.S)
        return json.loads(m.group(1))['props']['pageProps'] if m else {}

    def page(entry):
        path = entry if '/' in entry else f'windows/{entry}'
        slug = path.rsplit('/', 1)[1]
        link = f'https://filecr.com/{path}/'
        try:
            post = data(link).get('post')
        except requests.HTTPError as e:   # بعض الصفحات خاصّة (404)، لكنّها تظهر في البحث
            if e.response is None or e.response.status_code != 404:
                raise
            q = slug.replace('-', ' ')
            post = next((x for x in data('https://filecr.com/search/', q=q).get('posts', [])
                         if x.get('slug') == slug), None)
            link = 'https://filecr.com/search/?q=' + requests.utils.quote(q)
        if not post or not post.get('updated_at'):
            raise ValueError(f'{slug}: لا بيانات البرنامج في الصفحة')
        return {'key': f"{entry}@{post['updated_at']}", 'group': entry, 'title': clean(post['title']),
                'url': link, 'date': utc(post['updated_at'])}

    def safe(slug):
        try:
            return page(slug), None
        except Exception as e:
            return None, f'{slug}: {e}'

    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(safe, src['programs']))
    fails = [e for _, e in res if e]
    if len(fails) == len(res):
        raise RuntimeError(f'كلّ البرامج أخفقت، أوّلها {fails[0]}')
    if fails:
        print(f'    FileCR: أخفق {len(fails)} من {len(res)}: {fails[:10]}')
    return sorted((it for it, _ in res if it), key=lambda x: x['date'], reverse=True)


def read_carateen(src):
    """carateen يكتب صفحته بالجافاسكربت وبياناته مشفّرة، فيُقرأ بمتصفّح خفيّ: روابط الحلقات /watch/sp/<العمل>/<الحلقة>."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except Exception:          # على الحاسوب بلا متصفّح Playwright: Chrome المثبّت
            b = p.chromium.launch(channel='chrome', headless=True)
        try:
            pg = b.new_page(locale='ar', user_agent=UA['User-Agent'])
            pg.goto(src['url'], timeout=60000, wait_until='networkidle')
            links = pg.eval_on_selector_all(
                'a[href*="/watch/"]', 'e => e.map(x => [x.href, x.innerText.replace(/\\s+/g, " ").trim()])')
        finally:
            b.close()
    out, seen = [], set()
    for href, text in links:
        # نصّ البطاقة يبدأ بتاريخ عرضها («الأمس»، «2026-11-07»…)، فيُحذف
        text = re.sub(r'^(اليوم|الأمس|أمس|منذ \S+( \S+)?|\d{4}-\d{2}-\d{2})\s+', '', text)
        if re.search(r'/watch/sp/\d+/\d+', href) and text and href not in seen:
            seen.add(href)
            out.append({'key': href, 'title': text, 'url': href})
    if not out:
        raise ValueError('لم أجد روابط حلقات في الصفحة')
    return out


def gh_get(path):
    hdr = {'Accept': 'application/vnd.github+json'}
    if os.environ.get('GITHUB_TOKEN'):
        hdr['Authorization'] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return get(f'https://api.github.com/{path}', headers=hdr).json()


def read_github_releases(src):
    """إصدارات المستودعات المميّزة بنجمة. والمستودع المميّز حديثًا تُسجَّل إصداراته الحاليّة بلا إرسال."""
    repos = gh_get(f"users/{src['user']}/starred?per_page=100")

    def releases(repo):
        rel = gh_get(f"repos/{repo['full_name']}/releases?per_page=5")
        return [{'key': f"{repo['full_name']}#{r['id']}", 'group': repo['full_name'],
                 'title': f"{repo['full_name']}: {clean(r.get('name') or r['tag_name'])}"
                          + (' (تجريبيّ)' if r.get('prerelease') else ''),
                 'url': r['html_url'], 'date': utc(r.get('published_at'))}
                for r in rel if not r.get('draft')]

    with ThreadPoolExecutor(6) as ex:
        out = [x for lst in ex.map(releases, repos) for x in lst]
    return sorted(out, key=lambda x: x['date'], reverse=True)


def yt_get(endpoint, key, **params):
    """طلبٌ إلى YouTube Data API. وكلّ طلبٍ وحدةٌ واحدة من الحصّة اليوميّة (10,000)."""
    r = requests.get(f'https://www.googleapis.com/youtube/v3/{endpoint}', timeout=30, params=dict(params, key=key))
    if r.status_code != 200:   # لا يُذكر الرابط، ففيه المفتاح
        try:
            why = r.json()['error']['errors'][0]['reason']
        except (ValueError, KeyError, IndexError):
            why = ''
        raise RuntimeError(f'API HTTP {r.status_code} {why}'.strip())
    return r.json()


_oauth = {}


def yt_token():
    """رمز دخولٍ إلى حسابك في يوتيوب من إذنك المحفوظ (YT_REFRESH_TOKEN)، أو '' إن لم يكن."""
    if 'tok' not in _oauth:
        _oauth['tok'] = ''
        cid, sec, ref = (os.environ.get(k, '').strip() for k in ('YT_CLIENT_ID', 'YT_CLIENT_SECRET', 'YT_REFRESH_TOKEN'))
        if cid and sec and ref:
            r = requests.post('https://oauth2.googleapis.com/token', timeout=30, data={
                'client_id': cid, 'client_secret': sec, 'refresh_token': ref, 'grant_type': 'refresh_token'})
            if r.ok:
                _oauth['tok'] = r.json()['access_token']
            else:   # لا يُطبع الردّ كلّه
                try:
                    why = r.json().get('error', '')
                except ValueError:
                    why = ''
                print(f'    إذن يوتيوب: HTTP {r.status_code} {why}'.rstrip())
    return _oauth['tok']


def yt_mine(method, endpoint, body=None, **params):
    """طلبٌ إلى YouTube Data API باسمك (لقوائم التشغيل)."""
    r = requests.request(method, f'https://www.googleapis.com/youtube/v3/{endpoint}', timeout=30, params=params,
                         json=body, headers={'Authorization': f'Bearer {yt_token()}'})
    if r.status_code != 200:
        try:
            why = r.json()['error']['errors'][0]['reason']
        except (ValueError, KeyError, IndexError):
            why = ''
        raise RuntimeError(f'HTTP {r.status_code} {why}'.strip())
    return r.json()


def my_playlists(cfg):
    """قوائم التشغيل التي لها أزرار: [(المعرّف، الاسم)]، أو [] بلا إذن. وplaylists في الإعدادات إمّا "all"
    (كلّ قوائم حسابك، فالقائمة التي تنشئها في يوتيوب يظهر زرّها وحده)، وإمّا أسماءٌ بعينها كما هي في يوتيوب."""
    if 'pl' not in _oauth:
        _oauth['pl'] = []
        names = cfg.get('playlists', 'all')
        if names and yt_token():
            have, page = {}, None
            try:
                while True:
                    j = yt_mine('GET', 'playlists', part='snippet', mine='true', maxResults=50,
                                **({'pageToken': page} if page else {}))
                    for it in j.get('items', []):
                        have.setdefault(it['snippet']['title'].strip(), it['id'])
                    page = j.get('nextPageToken')
                    if not page:
                        break
            except Exception as e:
                print(f'    قوائم التشغيل: تعذّرت قراءتها ({e})')
            for n in (list(have) if names == 'all' else names)[:MAX_PLAYLISTS]:
                if n in have:
                    _oauth['pl'].append((have[n], n))
                elif have:
                    print(f'    قوائم التشغيل: لا قائمة باسم «{n}» في حسابك')
    return _oauth['pl']


def yt_buttons(cfg, it):
    """أزرار «أضف إلى قائمة» تحت فيديو يوتيوب، أو None. والضغطات يعالجها كلّ دقيقة apps-script/Code.gs
    على حسابك في Google، لا هذا البرنامج."""
    if not it['key'].startswith('yt:video:'):
        return None
    vid = it['key'][9:]
    btns = [{'text': f'➕ {name}', 'callback_data': f'p:{pid}:{vid}'} for pid, name in my_playlists(cfg)
            if len(f'p:{pid}:{vid}'.encode()) <= 64]
    return {'inline_keyboard': [btns[i:i + 2] for i in range(0, len(btns), 2)]} if btns else None


def yt_subscriptions(who, key):
    """القنوات التي يشترك فيها صاحب القناة who (رابطها أو معرّفها UC… أو @اسمها)، وتلزم أن تكون اشتراكاته عامّة."""
    m = re.search(r'(UC[\w-]{22})', who)
    if m:
        me = m.group(1)
    else:
        h = re.search(r'@([\w.\-]+)', who)
        if not h:
            raise RuntimeError('YT_SUBSCRIBER ليس رابط قناة ولا معرّفها')
        found = yt_get('channels', key, part='id', forHandle='@' + h.group(1)).get('items', [])
        if not found:
            raise RuntimeError('لم أجد القناة المذكورة في YT_SUBSCRIBER')
        me = found[0]['id']
    ids, page = [], None
    while True:
        j = yt_get('subscriptions', key, part='snippet', channelId=me, maxResults=50, order='alphabetical',
                   **({'pageToken': page} if page else {}))
        ids += [it['snippet']['resourceId']['channelId'] for it in j.get('items', [])]
        page = j.get('nextPageToken')
        if not page:
            break
    if not ids:
        raise RuntimeError('قائمة الاشتراكات فارغة')
    return list(dict.fromkeys(ids))


def yt_api(cid, key):
    """آخر فيديوهات القناة: قائمة رفعها (UU…)."""
    j = yt_get('playlistItems', key, part='snippet', playlistId='UU' + cid[2:], maxResults=15)
    out = []
    for it in j.get('items', []):
        sn = it['snippet']
        vid = sn.get('resourceId', {}).get('videoId')
        if vid:
            out.append({'key': f'yt:video:{vid}', 'url': f'https://www.youtube.com/watch?v={vid}',
                        'title': clean(sn.get('title')), 'author': clean(sn.get('channelTitle')),
                        'date': utc(sn.get('publishedAt'))})
    return out


def read_youtube(src):
    """آخر فيديوهات كلّ قناةٍ تشترك فيها. القنوات من اشتراكاتك العامّة إن كان YT_SUBSCRIBER والمفتاح، فتتحدّث
    القائمة وحدها، وإلّا (أو إن تعذّرت قراءتها) فمن YT_CHANNELS. والفيديوهات من YouTube Data API إن كان المفتاح
    YT_API_KEY، وإلّا من خلاصة القناة العامّة (وهي تتعطّل عند يوتيوب من حينٍ لآخر).
    والقناة المضافة حديثًا تُسجَّل فيديوهاتها الحاليّة بلا إرسال."""
    key = os.environ.get('YT_API_KEY', '').strip()
    who = os.environ.get('YT_SUBSCRIBER', '').strip()
    ids = [c for c in re.split(r'[\s,;]+', os.environ.get('YT_CHANNELS', '')) if c.startswith('UC')]
    if who and key:
        try:
            ids = yt_subscriptions(who, key)
            print(f'    يوتيوب: {len(ids)} قناة من اشتراكاتك')
        except Exception as e:
            if not ids:
                raise
            print(f'    يوتيوب: تعذّرت قراءة اشتراكاتك ({e})، فالقنوات من YT_CHANNELS ({len(ids)})')
    if not ids:
        return None   # لا قنوات بعد: يُتخطّى المصدر

    def feed(cid):
        pos = ids.index(cid) + 1   # رقم القناة في القائمة، لا معرّفها: سجلّ التشغيل عامّ
        try:
            if key:
                items = yt_api(cid, key)
            else:
                items = read_rss({'url': f'https://www.youtube.com/feeds/videos.xml?channel_id={cid}'})
            return [dict(x, group=cid, title=f"{x['author']}: {x['title']}" if x.get('author') else x['title'])
                    for x in items], None
        except Exception as e:
            return [], f'#{pos}: {e}'

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(feed, ids))
    fails = [e for _, e in res if e]
    if len(fails) == len(ids):
        raise RuntimeError(f'كلّ القنوات أخفقت، أوّلها {fails[0]}')
    if fails:
        print(f'    يوتيوب: أخفقت {len(fails)} من {len(ids)} قناة (رقمها في القائمة): {fails[:20]}')
    return [x for lst, _ in res for x in lst]


def read_tvmaze(src):
    """حلقات مسلسلٍ عُرضت من TVmaze (بديل telepisodes المحجوب)."""
    show = get(f"https://api.tvmaze.com/shows/{src['show_id']}").json()
    eps = get(f"https://api.tvmaze.com/shows/{src['show_id']}/episodes").json()
    now = datetime.now(timezone.utc).isoformat()
    out = []
    for e in eps:
        if e.get('airstamp') and e['airstamp'] <= now:
            url = (src['episode_link'].format(season=e['season'], number=e.get('number') or 0)
                   if src.get('episode_link') else src.get('link') or e['url'])
            out.append({'key': str(e['id']), 'url': url, 'date': utc(e['airstamp']),
                        'title': f"{show['name']} S{e['season']:02d}E{(e.get('number') or 0):02d}: {clean(e['name'])} ({e['airdate']})"})
    return out[::-1]


def read_page_links(src):
    """صفحة عاديّة: روابطها التي تطابق نمطًا (pattern)، ومعها pages صفحةً إن ذُكرت."""
    from bs4 import BeautifulSoup
    r = get(src['url'])
    soup = BeautifulSoup(r.text, 'html.parser')
    pat = re.compile(src.get('pattern', '.'))
    if not any(pat.search(a['href']) for a in soup.select('a[href]')):
        host = re.sub(r'^https?://([^/]+).*', r'\1', r.url)
        sample = list(dict.fromkeys(a['href'] for a in soup.select('a[href]') if host in a['href']))[-30:]
        raise ValueError(f'لا روابط تطابق النمط. العنوان: {clean(soup.title.string if soup.title else "")!r}، '
                         f'الرابط النهائيّ: {r.url}، الحجم: {len(r.text)}، أمثلة: {sample}')
    out, seen = [], set()

    def collect(soup):
        n = 0
        for a in soup.select('a[href]'):
            href, text = a['href'], clean(a.get_text())
            if pat.search(href) and text and href not in seen:
                seen.add(href)
                out.append({'key': href, 'title': text, 'url': href})
                n += 1
        return n

    collect(soup)
    # pages: صفحاتٌ تالية تُقرأ أيضًا (url/page/2/ …)، فلا يضيع ما يُنشر دفعةً واحدة أكثر من صفحة
    for n in range(2, src.get('pages', 1) + 1):
        try:
            nxt = BeautifulSoup(get(f"{src['url'].rstrip('/')}/page/{n}/").text, 'html.parser')
        except Exception as e:
            print(f"    {src['id']}: الصفحة {n}: {e}")
            break
        if not collect(nxt):
            break
    return out


READERS = {'rss': read_rss, 'anikoto': read_anikoto, 'filecr_pages': read_filecr_pages, 'carateen': read_carateen,
           'github_releases': read_github_releases, 'youtube': read_youtube, 'tvmaze': read_tvmaze,
           'page_links': read_page_links}


# ------------------------------------------------------------------ تيلغرام

def tg_send(text, preview=False, markup=None):
    token, chat = os.environ['TELEGRAM_TOKEN'], os.environ['TELEGRAM_CHAT_ID']
    data = {'chat_id': chat, 'text': text[:4000], 'parse_mode': 'HTML',
            'link_preview_options': json.dumps({'is_disabled': not preview})}
    if markup:
        data['reply_markup'] = json.dumps(markup)
    for _ in range(5):
        try:
            j = requests.post(f'https://api.telegram.org/bot{token}/sendMessage', timeout=30, data=data).json()
        except Exception as e:
            print('send:', e)
            time.sleep(5)
            continue
        if j.get('ok'):
            time.sleep(1.1)
            return True
        ra = (j.get('parameters') or {}).get('retry_after')
        if not ra:
            print('send failed:', j)
            return False
        time.sleep(ra + 1)
    return False


def fmt_item(src, it):
    return f"<b>{html.escape(src['name'])}</b>\n<a href=\"{html.escape(it['url'], True)}\">{html.escape(it['title'])}</a>"


def fmt_batch(src, items):
    lines = [f"<b>{html.escape(src['name'])}</b> — {len(items)} جديد"]
    for it in items[:40]:
        lines.append(f"• <a href=\"{html.escape(it['url'], True)}\">{html.escape(it['title'])}</a>")
    if len(items) > 40:
        lines.append(f'… و{len(items) - 40} غيرها')
    return '\n'.join(lines)


# ------------------------------------------------------------------ التشغيل

def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def run(args):
    cfg = load_json(CFG_PATH, None)
    state = load_json(STATE_PATH, {})
    state.setdefault('seen', {})
    state.setdefault('fail', {})
    state.setdefault('groups', {})
    send = not (args.dry or args.probe)
    total = 0
    for src in cfg['sources']:
        if not src.get('enabled', True) and not args.probe:
            continue
        sid = src['id']
        t0 = time.time()
        try:
            items = READERS[src['type']](src)
        except Exception as e:
            n = state['fail'].get(sid, 0) + 1
            print(f'✗ {sid}: {type(e).__name__}: {str(e) if args.probe else str(e)[:200]}')
            if send:
                state['fail'][sid] = n
                if n == src.get('fail_alert', FAIL_ALERT):
                    tg_send(f"⚠️ لم أستطع فحص «{html.escape(src['name'])}» في آخر {n} محاولات "
                            f"({html.escape(type(e).__name__)}). سأستمرّ في المحاولة.")
            continue
        if items is None:
            print(f'– {sid}: لا إعداد بعد، تُخطّي')
            continue
        if send and state['fail'].get(sid, 0) >= src.get('fail_alert', FAIL_ALERT):
            tg_send(f"✅ عاد فحص «{html.escape(src['name'])}» يعمل.")
        state['fail'].pop(sid, None)

        seen_list = state['seen'].get(sid, [])
        seen = set(seen_list)
        groups = set(state['groups'].get(sid, []))
        fresh = [it for it in items if it['key'] not in seen]
        if 'group' in (items[0] if items else {}):
            # مجموعةٌ جديدة (قناة أو مستودع أُضيف): تُسجَّل عناصرها بلا إرسال
            fresh = [it for it in fresh if it['group'] in groups]
            groups |= {it['group'] for it in items}
        if not seen_list:
            fresh = []          # أوّل فحصٍ للمصدر: تسجيلٌ بلا إرسال
        dt = time.time() - t0
        print(f'✓ {sid}: {len(items)} عنصرًا، الجديد {len(fresh)}' + (' (أوّل فحص: تسجيل فقط)' if not seen_list else '')
              + f' [{dt:.1f}s]')
        if args.probe or args.dry:
            for it in (items if args.probe else fresh)[:3 if args.probe else 50]:
                print('    ', it['title'][:110], '|', it['url'][:90])
        if not send:
            continue
        fresh.reverse()  # الأقدم أوّلًا
        ok = True
        if len(fresh) > src.get('max_single', MAX_SINGLE):
            ok = tg_send(fmt_batch(src, fresh))
        else:
            for it in fresh:
                ok = tg_send(fmt_item(src, it), preview=src.get('preview', False), markup=yt_buttons(cfg, it)) and ok
        if not ok:
            print(f'  لم يُرسل كلّ جديد {sid}، فلن يُسجَّل حتّى يُرسل في الفحص القادم')
            continue
        total += len(fresh)
        keys = [it['key'] for it in items] + [k for k in seen_list if k not in {it['key'] for it in items}]
        state['seen'][sid] = keys[:max(KEEP_KEYS, len(items))]
        if groups:
            state['groups'][sid] = sorted(groups)
    if send:
        state['last_run'] = datetime.now().isoformat(timespec='seconds')
        tmp = STATE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding='utf-8')
        tmp.replace(STATE_PATH)
        print(f'أُرسل {total} عنصرًا.')


def hours_ar(h):
    h = int(h) if float(h).is_integer() else h
    if h == 1:
        return 'ساعة'
    if h == 2:
        return 'ساعتين'
    return f'{h} ساعات' if isinstance(h, int) and 3 <= h <= 10 else f'{h} ساعة'


def send_recent(hours, dry=False):
    """يرسل ما نُشر في آخر «hours» ساعة من كلّ مصدرٍ له تاريخ نشر، ولا يمسّ state.json.
    والمصدر الذي لا تاريخ فيه يُذكر في آخر رسالة."""
    cfg = load_json(CFG_PATH, None)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec='seconds')
    out = tg_send if not dry else (lambda t, preview=False, markup=None: print('---\n' + t) or True)
    out(f'🕒 ما نُشر في آخر {hours_ar(hours)}')
    undated, total = [], 0
    for src in cfg['sources']:
        if not src.get('enabled', True):
            continue
        try:
            items = READERS[src['type']](src)
        except Exception as e:
            print(f"✗ {src['id']}: {type(e).__name__}: {str(e)[:200]}")
            out(f"⚠️ «{html.escape(src['name'])}»: تعذّر فحصه الآن.")
            continue
        if items is None:
            continue
        if not any(it.get('date') for it in items):
            undated.append(src['name'])
            continue
        recent = sorted((it for it in items if it.get('date', '') >= cutoff), key=lambda it: it['date'])
        print(f"✓ {src['id']}: {len(recent)} في آخر {hours_ar(hours)}")
        total += len(recent)
        if len(recent) > src.get('max_single', MAX_SINGLE):
            out(fmt_batch(src, recent))
        else:
            for it in recent:
                out(fmt_item(src, it), preview=src.get('preview', False), markup=yt_buttons(cfg, it))
    note = f'انتهى: {total} عنصرًا.'
    if undated:
        note += ('\nهذه المواقع لا تذكر وقت النشر، فلا يُعرف منها ما نُشر في ساعاتٍ بعينها، '
                 'ويصلك جديدها في الفحص المعتاد كلّ ساعة: ' + '، '.join(undated))
    out(note)


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--probe', action='store_true')
    ap.add_argument('--test', action='store_true')
    ap.add_argument('--recent', type=float, metavar='HOURS',
                    help='أرسل ما نُشر في آخر عددٍ من الساعات (مع --dry: اطبعه فقط)')
    args = ap.parse_args()
    if args.test:
        sys.exit(0 if tg_send('✅ بوت المواقع يعمل.') else 1)
    if args.recent:
        send_recent(args.recent, args.dry)
        return
    run(args)


if __name__ == '__main__':
    main()
