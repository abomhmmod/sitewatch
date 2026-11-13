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
MAX_SINGLE = 10          # إن زاد جديد يوتيوب على هذا (أو max_single) جُمع كرسائل المواقع
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
    series_list: صفحةٌ فيها أسماء المسلسلات المطلوبة (مثل قائمة الكرتون في WCO)، فلا يبقى إلّا ما تصنيفه منها.
    extra_urls: خلاصاتٌ أخرى تُضمّ إليها، و{year} فيها السنة الحاليّة (مثل تصنيف «مسلسلات رمضان» لكلّ سنة).
                والتي لا توجد بعد (404) تُتخطّى."""
    def feed(url):
        out = rss_page(url)
        if src.get('pages_hours'):
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=src['pages_hours'])).isoformat()
            sep = '&' if '?' in url else '?'
            for n in range(2, src.get('max_pages', 10) + 1):
                dates = [it['date'] for it in out if it.get('date')]
                if not dates or min(dates) < cutoff:
                    break
                more = rss_page(f"{url}{sep}paged={n}")
                if not more:
                    break
                out += more
        return out

    out = feed(src['url'])
    for u in src.get('extra_urls', []):
        try:
            out += feed(u.format(year=datetime.now().year))
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 404:
                raise
    if src.get('extra_urls'):
        out.sort(key=lambda it: it.get('date', ''), reverse=True)
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
            return [dict(x, group=cid, vtitle=x['title'],
                         title=f"{x['author']}: {x['title']}" if x.get('author') else x['title'])
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
    """صفحة عاديّة: روابطها التي تطابق نمطًا (pattern)، ومعها pages صفحةً إن ذُكرت.
    title_sel: إن ذُكر أُخذ العنوان من هذا العنصر داخل الرابط، لا من نصّ الرابط كلّه.
    title_attr: إن ذُكر أُخذ العنوان من سمة الرابط هذه (title مثلًا)، لا من نصّه."""
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
            href = a['href']
            if src.get('title_attr'):
                text = clean(a.get(src['title_attr']))
            elif src.get('title_sel'):
                el = a.select_one(src['title_sel'])
                text = clean(el.get_text()) if el else ''
            else:
                text = clean(a.get_text())
            if pat.search(href) and text and href not in seen:
                seen.add(href)
                out.append({'key': href, 'title': text, 'url': href})
                n += 1
        return n

    collect(soup)
    if not out:     # روابط تطابق النمط لكن بلا عنوان (تغيّر شكل الصفحة): فحصٌ متعذّر، لا «لا جديد»
        raise ValueError(f'روابط تطابق النمط لكن بلا عنوان (title_sel={src.get("title_sel")!r}, title_attr={src.get("title_attr")!r})، فلعلّ شكل الصفحة تغيّر')
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


def read_stardima(src):
    """ستارديما: صفحة «الجديد» فيها آخر حلقةٍ من كلّ عمل فقط. فالبطاقة الجديدة تُفتح صفحتها لمعرفة موسمها،
    ثمّ تُؤخذ من قائمة حلقات الموسم (/series/season/<id>) كلّ حلقةٍ بعد آخر حلقةٍ معروفة من العمل،
    فلا يضيع ما نُشر من حلقاتٍ عدّة بين فحصين. وفي أوّل فحص (لا معروف) تكفي البطاقات."""
    cards = read_page_links(src)
    known = src.get('_known') or set()
    if not known:
        return cards
    out = []
    for card in cards:
        m = re.search(r'/tvshow/([^/]+)/play/(\d+)$', card['url'])
        if not m or same(card['key']) in known:
            out.append(card)
            continue
        show = m.group(1)
        mine = {int(x) for k in known for x in re.findall(rf'/tvshow/{re.escape(show)}/play/(\d+)$', k)}
        try:
            sid = re.search(r'data-initial-season-id="(\d+)"', get(card['url']).text).group(1)
            base = re.sub(r'^(https?://[^/]+).*', r'\1', card['url'])
            eps = get(f'{base}/series/season/{sid}', headers={'Accept': 'application/json',
                                                              'X-Requested-With': 'XMLHttpRequest'}).json()['episodes']
        except Exception as e:
            print(f"    stardima: تعذّرت قائمة حلقات {card['title']}: {type(e).__name__}، فتُرسل البطاقة وحدها")
            out.append(card)
            continue
        ids = [e['id'] for e in eps]
        last = max((i for i, x in enumerate(ids) if x in mine), default=-1)
        name = re.sub(r'\s*-\s*حلقة\s*\d+\s*$', '', card['title'])
        new = [e for e in eps[last + 1:] if e['id'] <= int(m.group(2))][-100:] or [None]
        for e in reversed(new):          # الأحدث أوّلًا، كترتيب الصفحة
            if e is None:
                out.append(card)
                continue
            url = re.sub(r'/play/\d+$', f"/play/{e['id']}", card['url'])
            out.append({'key': url, 'title': f"{name} - حلقة {e['episode_number']}", 'url': url})
    return out


READERS = {'rss': read_rss, 'anikoto': read_anikoto, 'filecr_pages': read_filecr_pages, 'carateen': read_carateen,
           'github_releases': read_github_releases, 'youtube': read_youtube, 'tvmaze': read_tvmaze,
           'page_links': read_page_links, 'stardima': read_stardima}


# ------------------------------------------------------------------ تيلغرام

def tg(method, **data):
    """طلبٌ إلى واجهة تيلغرام، مع الانتظار إن طلبت ذلك (429)."""
    token = os.environ['TELEGRAM_TOKEN']
    data = {k: json.dumps(v) if isinstance(v, (dict, list, bool)) else v for k, v in data.items()}
    for _ in range(5):
        try:
            j = requests.post(f'https://api.telegram.org/bot{token}/{method}', timeout=30, data=data).json()
        except Exception as e:
            print(f'{method}:', e)
            time.sleep(5)
            continue
        ra = (j.get('parameters') or {}).get('retry_after')
        if j.get('ok') or not ra:
            return j
        time.sleep(ra + 1)
    return {'ok': False, 'description': 'لم يُرسل بعد عدّة محاولات'}


def tg_post(chat, text, preview=False, markup=None, silent=False, thread=None):
    """رسالةٌ إلى محادثة، أو إلى موضوعٍ فيها؛ ويعيد ردّ تيلغرام كما هو."""
    data = {'chat_id': chat, 'text': text[:4096], 'parse_mode': 'HTML',
            'link_preview_options': {'is_disabled': not preview}}
    if thread:
        data['message_thread_id'] = thread
    if markup:
        data['reply_markup'] = markup
    if silent:
        data['disable_notification'] = True
    j = tg('sendMessage', **data)
    if j.get('ok'):
        time.sleep(1.1)
    return j


def tg_send(text, preview=False, markup=None, silent=False):
    """رسالةٌ إلى محادثتك الخاصّة مع البوت."""
    j = tg_post(os.environ['TELEGRAM_CHAT_ID'], text, preview, markup, silent)
    if not j.get('ok'):
        print('send failed:', j)
        return False
    return j['result']['message_id']


THREAD_GONE = re.compile(r'thread not found|TOPIC_DELETED|TOPIC_CLOSED|TOPIC_ID_INVALID', re.I)
GROUP_GONE = re.compile(r'chat not found|not enough rights|not a forum|have no rights|CHAT_ADMIN_REQUIRED|'
                        r'administrator rights|CHAT_WRITE_FORBIDDEN|bot is not a member|group chat was upgraded', re.I)


class Group:
    """مجموعة المواضيع (السرّ TELEGRAM_GROUP_ID): موضوعٌ لكلّ موقع يصنعه البوت، و«حال المواقع» والتنبيهات في General.
    فإن لم تُضبط، أو رفضت الإرسال، وصل كلّ شيءٍ إلى المحادثة الخاصّة كما كان، مع تنبيهٍ واحد."""

    def __init__(self, state, create=True):
        self.id = os.environ.get('TELEGRAM_GROUP_ID', '').strip()
        self.create = create     # send-recent لا يحفظ state.json، فلا يصنع مواضيع: ما لا موضوع له يذهب إلى General
        self.skip = False        # رفضت المجموعة في هذا الفحص، فلا تُجرَّب ثانيةً حتّى الفحص القادم
        g = state.get('group') or {}
        if g.get('id') != self.id:   # مجموعةٌ أخرى: مواضيعها غير مواضيع القديمة
            g = {'id': self.id, 'topics': {}, 'unpin': []}
        if create:
            state['group'] = g
        self.g = g

    @property
    def on(self):
        return bool(self.id) and not self.skip

    def refused(self, j):
        """هل الخطأ من المجموعة نفسها (طُرد البوت، أو لا صلاحيّة، أو ليست منتدى)؟ فيُنبَّه مرّةً ويُرجع إلى الخاصّ."""
        why = j.get('description', '')
        if j.get('error_code') != 403 and not GROUP_GONE.search(why):
            return False
        print('المجموعة رفضت:', why)
        self.skip = True
        if not self.g.get('down'):
            self.g['down'] = True
            tg_send(f'⚠️ مجموعة المواضيع رفضت الإرسال ({html.escape(why[:150])})، '
                    'فصار الجديد يصلك هنا حتّى تُصلَح. أعد البوت مشرفًا فيها بصلاحيّة إدارة المواضيع.')
        return True

    def worked(self):
        if self.g.pop('down', None):
            tg_send('✅ عادت مجموعة المواضيع تعمل، فصار الجديد يصل إليها.')

    def topic(self, src):
        """رقم موضوع الموقع (يُصنع إن لم يكن)، أو None لـ General، أو False إن رفضت المجموعة."""
        sid = src['id']
        if sid in self.g['topics'] or not self.create:
            return self.g['topics'].get(sid)
        j = tg('createForumTopic', chat_id=self.id, name=f"{src.get('icon', '🔔')} {src['name']}"[:128])
        if not j.get('ok'):
            if self.refused(j):
                return False
            print(f'تعذّر صنع موضوع {sid}:', j.get('description'))
            return None
        self.g['topics'][sid] = j['result']['message_thread_id']
        self.g['unpin'].append(sid)       # تيلغرام يثبّت أوّل رسالةٍ في الموضوع الجديد، فيُلغى تثبيتها بعدها
        return self.g['topics'][sid]

    def ensure_topics(self, cfg):
        """مواضيع كلّ المواقع المفعّلة، بترتيب الإعداد، فتظهر كلّها من أوّل فحص (ومعها موضوع Shorts يوتيوب)."""
        for src in cfg['sources']:
            if src.get('enabled', True):
                for s in (src, shorts_of(src)):
                    if s and self.on:
                        self.topic(s)

    def send(self, src, text, preview=False, markup=None, silent=False):
        """جديد الموقع إلى موضوعه؛ ويعيد رقم الرسالة أو False كما تفعل tg_send."""
        for attempt in (1, 2):
            if not self.on:
                break
            tid = self.topic(src)
            if tid is False:
                break
            j = tg_post(self.id, text, preview, markup, silent, thread=tid)
            if j.get('ok'):
                self.worked()
                if tid and src['id'] in self.g['unpin']:
                    tg('unpinAllForumTopicMessages', chat_id=self.id, message_thread_id=tid)
                    self.g['unpin'].remove(src['id'])
                return j['result']['message_id']
            if attempt == 1 and tid and THREAD_GONE.search(j.get('description', '')):
                print(f'موضوع {src["id"]} حُذف، فيُصنع من جديد')
                self.g['topics'].pop(src['id'], None)
                continue
            if self.refused(j):
                break
            print('send failed:', j)
            return False
        return tg_send(text, preview, markup, silent)

    def general(self, text, silent=False):
        """رسالةٌ إلى General في المجموعة، أو إلى الخاصّ إن لم تكن."""
        if self.on:
            j = tg_post(self.id, text, silent=silent)
            if j.get('ok'):
                self.worked()
                return j['result']['message_id']
            if not self.refused(j):
                print('send failed:', j)
                return False
        return tg_send(text, silent=silent)

    def alert(self, text):
        """تنبيهٌ إلى الخاصّ وإلى General معًا."""
        ok = tg_send(text)
        if self.on:
            self.refused(tg_post(self.id, text))
        return ok


def a(url, text):
    return f'<a href="{html.escape(url, True)}">{html.escape(text, False)}</a>'


EP_PATTERNS = [
    re.compile(r'^(?P<s>.+?)[\s\-—:|]*(?:الحلقة|حلقة)\s*(?P<e>\d+)'),
    re.compile(r'^(?P<s>.+?)\s+(?:Season\s+(?P<season>\d+)\s+)?Episode\s+(?P<e>\d+)', re.I),
    re.compile(r'^(?P<s>.+?)\s+S(?P<season>\d+)E(?P<e>\d+)', re.I),
]
PREFIX = re.compile(r'^(?:مشاهدة\s+)?(?:(?:انمي|أنمي|مسلسل|برنامج)\s+)?')
FINAL = re.compile(r'ال[اأ]خيرة')


def tidy(title):
    """العنوان بلا «مسلسل/انمي» في أوّله ولا «مترجم اون لاين» في آخره."""
    t = PREFIX.sub('', title)
    t = re.sub(r'\s+(?:مترجم[ةه]?)?(?:\s*(?:اون|أون)\s+لاين)?\s*$', '', t)
    return t.strip() or title


def split_episode(title):
    """(المسلسل، رقم الحلقة، تتمّة العنوان بعد «:» إن كانت) من عنوان حلقة، أو (None, None, '')."""
    for pat in EP_PATTERNS:
        m = pat.search(title)
        if m:
            name = PREFIX.sub('', m.group('s')).strip(' -—:|')
            if m.groupdict().get('season'):
                name += f" الموسم {int(m.group('season'))}"
            if 'مدبلج' in title:
                name += ' (مدبلج)'
            rest = title[m.end():].strip()
            return name, int(m.group('e')), rest if rest.startswith(':') else ''
    return None, None, ''


def ranges(nums):
    nums, out = sorted(set(nums)), []
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return '، '.join(f'{x}' if x == y else f'{x}–{y}' for x, y in out)


def site_lines(items):
    """أسطر رسالة الموقع: حلقات المسلسل الواحد في سطرٍ واحد، برابط أحدثها."""
    groups, order = {}, []
    for it in items:
        name, ep, rest = split_episode(it['title'])
        k = name or ('\0' + it['key'])
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append((ep, rest, it))
    lines = []
    for k in order:
        g = groups[k]
        ep, rest, last = max(g, key=lambda x: x[0] or 0)
        final = ' (الأخيرة)' if FINAL.search(last['title']) else ''
        if ep is None:
            lines.append('• ' + a(last['url'], tidy(last['title'])[:200]))
        elif len(g) == 1:
            lines.append(f"• {a(last['url'], k)} — الحلقة {ep}{html.escape(rest, False)}{final}")
        else:
            lines.append(f"• {a(last['url'], k)} — الحلقات {ranges(e for e, _, _ in g)}{final}")
    return lines


def times_ar(n):
    return 'مرّة' if n == 1 else 'مرّتين' if n == 2 else f'{n} مرّات' if n <= 10 else f'{n} مرّة'


def count_ar(n):
    return 'جديدٌ واحد' if n == 1 else ('جديدان' if n == 2 else f'{n} جديدة' if n <= 10 else f'{n} جديدًا')


def fmt_site(src, items, limit=3500):
    """جديد الموقع في رسالةٍ واحدة (أو أكثر إن طال)، والأقدم أوّلًا."""
    head = f"{src.get('icon', '🔔')} <b>{html.escape(src['name'])}</b> · {count_ar(len(items))}"
    msgs, cur = [], head
    for line in site_lines(items):
        if len(cur) + len(line) + 1 > limit:
            msgs.append(cur)
            cur = f"{src.get('icon', '🔔')} <b>{html.escape(src['name'])}</b> (تابع)"
        cur += '\n' + line
    msgs.append(cur)
    return msgs


def iso_secs(d):
    m = re.fullmatch(r'P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', d or '')
    if not m:
        return None
    dd, h, mi, se = (int(x or 0) for x in m.groups())
    return ((dd * 24 + h) * 60 + mi) * 60 + se


def clock(secs):
    h, r = divmod(secs, 3600)
    return f'{h}:{r // 60:02d}:{r % 60:02d}' if h else f'{r // 60}:{r % 60:02d}'


def yt_details(items):
    """مدّة كلّ فيديو، وهل هو بثٌّ أو عرضٌ قادم أو Short: {المعرّف: {...}}. وحدةٌ لكلّ 50 فيديو."""
    key = os.environ.get('YT_API_KEY', '').strip()
    ids = [it['key'][9:] for it in items if it['key'].startswith('yt:video:')]
    out = {}
    if not key or not ids:
        return out
    try:
        for i in range(0, len(ids), 50):
            j = yt_get('videos', key, part='contentDetails,snippet,liveStreamingDetails', id=','.join(ids[i:i + 50]))
            for v in j.get('items', []):
                secs = iso_secs(v.get('contentDetails', {}).get('duration'))
                live = v.get('snippet', {}).get('liveBroadcastContent', 'none')
                start = (v.get('liveStreamingDetails') or {}).get('scheduledStartTime')
                short = False
                if secs and secs <= 180 and live == 'none':   # Short: رابط /shorts/ يُفتح ولا يُحوَّل
                    try:
                        r = requests.head(f"https://www.youtube.com/shorts/{v['id']}", headers=UA, timeout=15,
                                          allow_redirects=False)
                        short = r.status_code == 200
                    except requests.RequestException:
                        pass
                out[v['id']] = {'secs': secs, 'live': live, 'start': start, 'short': short}
    except Exception as e:
        print('    تفاصيل الفيديوهات:', e)
    return out


def fmt_video(src, it, d):
    """رسالة فيديو يوتيوب: القناة ونوع الفيديو، ثمّ العنوان، ثمّ مدّته أو موعده."""
    d = d or {}
    tag = {'live': ' · 🔴 بثٌّ مباشر', 'upcoming': ' · 📅 عرضٌ قادم'}.get(d.get('live'), ' · Short' if d.get('short') else '')
    lines = [f"{src.get('icon', '📺')} <b>{html.escape(it.get('author') or src['name'])}</b>{tag}",
             a(it['url'], it.get('vtitle') or it['title'])]
    if d.get('live') == 'upcoming' and d.get('start'):
        st = datetime.fromisoformat(d['start'].replace('Z', '+00:00')).astimezone()
        lines.append(f'🕒 يبدأ {day_time(st)}')
    elif d.get('secs'):
        lines.append(f"⏱ {clock(d['secs'])}")
    return '\n'.join(lines)


DAYS = ['الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد']


def day_time(t):
    """«14:05» إن كان اليوم، و«أمس 14:05»، وإلّا «الخميس 14:05» في الأسبوع، أو التاريخ."""
    now = datetime.now(t.tzinfo)
    days = (now.date() - t.date()).days
    hm = t.strftime('%H:%M')
    if days == 0:
        return hm
    if days == 1:
        return f'أمس {hm}'
    if days == -1:
        return f'غدًا {hm}'
    if -6 <= days < 7:
        return f'{DAYS[t.weekday()]} {hm}'
    return t.strftime('%Y-%m-%d ') + hm


def shorts_of(src):
    """موضوع Shorts يوتيوب (إعداد «shorts» في مصدر يوتيوب): وجهة إرسالٍ فقط، ومفاتيحه وحاله في مصدر يوتيوب نفسه."""
    s = src.get('shorts') if src.get('type') == 'youtube' else None
    if not s:
        return None
    return {'id': s.get('id', src['id'] + '-shorts'), 'icon': s.get('icon', '📱'),
            'name': s.get('name', src['name'] + ' · Shorts'), 'type': 'youtube'}


def send_new(cfg, src, items, out=None):
    """يرسل جديد مصدرٍ (الأقدم أوّلًا) بـ out(وجهة، نصّ): فيديوهات يوتيوب كلٌّ في رسالة بأزرارها،
    والـShorts في موضوعها إن كان لها موضوع؛ وغيرها رسالةٌ للموقع."""
    out = out or (lambda to, t, **k: tg_send(t, **k))
    ok = True
    if src['type'] != 'youtube':
        one = len(items) == 1 and src.get('preview', False)
        for m in fmt_site(src, items):
            ok = bool(out(src, m, preview=one)) and ok
        return ok
    det = yt_details(items)
    shorts = shorts_of(src)
    parts = {}          # الوجهة ← فيديوهاتها، بترتيبها
    for it in items:
        to = shorts if shorts and (det.get(it['key'][9:]) or {}).get('short') else src
        parts.setdefault(to['id'], (to, []))[1].append(it)
    for to, part in parts.values():
        print(f"    {to['id']}: {len(part)}")
        if len(part) <= src.get('max_single', MAX_SINGLE):
            for it in part:
                ok = bool(out(to, fmt_video(src, it, det.get(it['key'][9:])), preview=True,
                              markup=yt_buttons(cfg, it))) and ok
        else:
            for m in fmt_site(to, part):
                ok = bool(out(to, m)) and ok
    return ok


def update_status(cfg, state, results, grp):
    """رسالة «حال المواقع» المثبّتة: تُعدَّل بصمت بعد كلّ فحص، وتُرسل وتُثبَّت إن لم تكن."""
    lines = [f'📌 <b>حال المواقع</b>', f'آخر فحص: {day_time(datetime.now().astimezone())}', '']
    for src in cfg['sources']:
        if not src.get('enabled', True):
            continue
        sid, r = src['id'], results.get(src['id'])
        name = f"{src.get('icon', '🔔')} {html.escape(src['name'])}"
        last = state.get('last_new', {}).get(sid)
        since = f' · آخر جديد {day_time(datetime.fromisoformat(last))}' if last else ''
        if r is None:
            continue
        if r[0] == 'new':
            lines.append(f'{name} — <b>{count_ar(r[1])}</b>')
        elif r[0] == 'none':
            lines.append(f'{name} — لا جديد{since}')
        elif r[0] == 'seed':
            lines.append(f'{name} — أوّل فحص: سُجّل ما فيه')
        else:
            lines.append(f'⚠️ {html.escape(src["name"])} — تعذّر فحصه آخر {times_ar(r[1])}{since}')
    text = '\n'.join(lines)
    # في General من المجموعة إن كانت، وإلّا في الخاصّ؛ وإن انتقلت حُذفت القديمة
    private = os.environ['TELEGRAM_CHAT_ID']
    chat = grp.id if grp.on else private
    mid, old = state.get('status_msg'), str(state.get('status_chat') or private)
    if mid and old == str(chat):
        j = tg('editMessageText', chat_id=chat, message_id=mid, text=text, parse_mode='HTML',
               link_preview_options={'is_disabled': True})
        if j.get('ok') or 'not modified' in j.get('description', ''):
            return
        print('رسالة الحال: تعذّر تعديلها، فتُرسل من جديد:', j.get('description'))
    elif mid and not grp.skip:      # انتقلت إلى المجموعة أو منها (لا لرفضٍ عابر في هذا الفحص): تُحذف القديمة
        if not tg('deleteMessage', chat_id=old, message_id=mid).get('ok'):
            tg('unpinChatMessage', chat_id=old, message_id=mid)
    j = tg_post(chat, text, silent=True)
    if not j.get('ok') and chat != private and grp.refused(j):
        chat = private
        j = tg_post(chat, text, silent=True)
    if j.get('ok'):
        mid = j['result']['message_id']
        state['status_msg'], state['status_chat'] = mid, str(chat)
        tg('pinChatMessage', chat_id=chat, message_id=mid, disable_notification=True)


# ------------------------------------------------------------------ التشغيل

def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def same(key):
    """مفتاح العنصر بلا اسم الموقع: المواقع تغيّر عناوينها (topcinemaa.com ← .vip)، فلا يُعدّ قديمها جديدًا."""
    return re.sub(r'^https?://[^/]+', '', key)


def run(args):
    cfg = load_json(CFG_PATH, None)
    state = load_json(STATE_PATH, {})
    state.setdefault('seen', {})
    state.setdefault('fail', {})
    state.setdefault('groups', {})
    state.setdefault('last_new', {})
    state.setdefault('off', [])
    send = not (args.dry or args.probe)
    grp = Group(state)
    if send:
        grp.ensure_topics(cfg)
    total = 0
    results = {}
    for src in cfg['sources']:
        sid = src['id']
        if not src.get('enabled', True) and not args.probe:
            if send and sid not in state['off']:
                state['off'].append(sid)     # فإن أُعيد يُسجَّل ما فيه بلا إرسال، فلا يصل قديمه
            continue
        seen_list = state['seen'].get(sid, [])
        seen = {same(k) for k in seen_list}
        t0 = time.time()
        try:
            items = READERS[src['type']]({**src, '_known': seen})
        except Exception as e:
            n = state['fail'].get(sid, 0) + 1
            print(f'✗ {sid}: {type(e).__name__}: {str(e) if args.probe else str(e)[:200]}')
            results[sid] = ('fail', n)
            if send:
                state['fail'][sid] = n
                if n == src.get('fail_alert', FAIL_ALERT):
                    grp.alert(f"⚠️ لم أستطع فحص «{html.escape(src['name'])}» في آخر {n} محاولات "
                            f"({html.escape(type(e).__name__)}). سأستمرّ في المحاولة.")
            continue
        if items is None:
            print(f'– {sid}: لا إعداد بعد، تُخطّي')
            continue
        if send and state['fail'].get(sid, 0) >= src.get('fail_alert', FAIL_ALERT):
            grp.alert(f"✅ عاد فحص «{html.escape(src['name'])}» يعمل.")
        state['fail'].pop(sid, None)

        # العنصر الواحد بعنوانين للموقع (أثناء انتقاله) عنصرٌ واحد
        uniq = set()
        items = [it for it in items if not (same(it['key']) in uniq or uniq.add(same(it['key'])))]
        groups = set(state['groups'].get(sid, []))
        fresh = [it for it in items if same(it['key']) not in seen]
        if 'group' in (items[0] if items else {}):
            # مجموعةٌ جديدة (قناة أو مستودع أُضيف): تُسجَّل عناصرها بلا إرسال
            fresh = [it for it in fresh if it['group'] in groups]
            groups |= {it['group'] for it in items}
        revived = sid in state['off'] and src.get('enabled', True)    # مصدرٌ أُعيد بعد إيقافه: ما فاته قديم
        seed = not seen_list or revived
        if seed:
            fresh = []          # أوّل فحصٍ للمصدر، أو أوّل فحصٍ بعد إعادته: تسجيلٌ بلا إرسال
        dt = time.time() - t0
        print(f'✓ {sid}: {len(items)} عنصرًا، الجديد {len(fresh)}'
              + (' (أُعيد بعد إيقافه: تسجيل فقط)' if revived else ' (أوّل فحص: تسجيل فقط)' if seed else '')
              + f' [{dt:.1f}s]')
        if args.probe or args.dry:
            for it in (items if args.probe else fresh)[:3 if args.probe else 50]:
                print('    ', it['title'][:110], '|', it['url'][:90])
        results[sid] = ('seed', 0) if seed else ('new', len(fresh)) if fresh else ('none', 0)
        if not send:
            continue
        fresh.reverse()  # الأقدم أوّلًا
        ok = send_new(cfg, src, fresh, grp.send) if fresh else True
        if fresh and ok:
            state['last_new'][sid] = datetime.now().astimezone().isoformat(timespec='seconds')
        if not ok:
            print(f'  لم يُرسل كلّ جديد {sid}، فلن يُسجَّل حتّى يُرسل في الفحص القادم')
            continue
        total += len(fresh)
        keys = [it['key'] for it in items] + [k for k in seen_list if same(k) not in uniq]
        state['seen'][sid] = keys[:max(KEEP_KEYS, len(items))]
        if groups:
            state['groups'][sid] = sorted(groups)
        if revived:
            state['off'].remove(sid)
    if send:
        state['last_run'] = datetime.now().isoformat(timespec='seconds')
        update_status(cfg, state, results, grp)
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
    grp = Group(load_json(STATE_PATH, {}), create=False)     # مواضيع المواقع من state.json، وGeneral للعناوين
    show = (lambda t, preview=False, markup=None, silent=False:
            print('---\n' + t + (f'\n[أزرار: {len(markup["inline_keyboard"])} صفّ]' if markup else '')) or True)
    out = show if dry else grp.general
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
        if recent:
            send_new(cfg, src, recent, (lambda to, t, **k: show(t, **k)) if dry else grp.send)
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
        ok = bool(tg_send('✅ بوت المواقع يعمل.'))
        grp = Group({}, create=False)
        if grp.id:      # والمجموعة: رسالةٌ في General
            j = tg_post(grp.id, '✅ بوت المواقع يعمل في مجموعة المواضيع.')
            print('المجموعة:', 'تعمل' if j.get('ok') else j.get('description'))
            ok = bool(j.get('ok')) and ok
        sys.exit(0 if ok else 1)
    if args.recent:
        send_recent(args.recent, args.dry)
        return
    run(args)


if __name__ == '__main__':
    main()
