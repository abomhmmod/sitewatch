# -*- coding: utf-8 -*-
"""
يبني السرّ MYWORKS لموضوع «My Works»: صورة قائمتيك في IMDb (لا تُقرآن من خوادم GitHub لأنّهما خاصّتان).

  1. من IMDb صدّر القائمتين (Watchlist و«Plan to Watch»): في صفحة كلّ قائمة زرّ Export، فينزل ملفّ CSV.
  2. python myworks_build.py ملف1.csv ملف2.csv
     (ويُقرأ مفتاح TMDB من المتغيّر TMDB_API_KEY)
  3. يكتب myworks.local.json بجانبه (لا يُرفع: في .gitignore). انسخ محتواه إلى سرّ المستودع MYWORKS
     (Settings ← Secrets and variables ← Actions).

يحفظ «salt» من myworks.local.json القديم إن كان: به تُموَّه مفاتيح state.json، فإن تغيّر عُدّ كلّ قديمٍ جديدًا.
"""
import csv
import json
import os
import secrets
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / 'myworks.local.json'
UA = {'User-Agent': 'SiteWatch/1.0 (https://github.com/abomhmmod/sitewatch)'}


def imdb_ids(paths):
    ids = set()
    for p in paths:
        with open(p, encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f):
                c = (row.get('Const') or '').strip()
                if c.startswith('tt'):
                    ids.add(c)
    return ids


def tmdb(ids, key):
    def one(i):
        for _ in range(3):
            try:
                j = requests.get(f'https://api.themoviedb.org/3/find/{i}', timeout=30,
                                 params={'api_key': key, 'external_source': 'imdb_id'}).json()
                if j.get('tv_results'):
                    return 'tv', j['tv_results'][0]['id']
                if j.get('movie_results'):
                    m = requests.get(f"https://api.themoviedb.org/3/movie/{j['movie_results'][0]['id']}",
                                     params={'api_key': key}, timeout=30).json()
                    return 'col', (m.get('belongs_to_collection') or {}).get('id')
                return None, None
            except requests.RequestException:
                pass
        return None, None
    cols, tvs = set(), set()
    with ThreadPoolExecutor(8) as ex:
        for kind, v in ex.map(one, sorted(ids)):
            if kind == 'tv':
                tvs.add(v)
            elif kind == 'col' and v:
                cols.add(v)
    return cols, tvs


def wikidata(ids):
    ids, qs = sorted(ids), set()
    for n in range(0, len(ids), 40):
        off = 0
        while off is not None:
            j = requests.get('https://www.wikidata.org/w/api.php', headers=UA, timeout=60, params={
                'action': 'query', 'list': 'search', 'format': 'json', 'srlimit': 500, 'sroffset': off,
                'srsearch': 'haswbstatement:' + '|'.join('P345=' + x for x in ids[n:n + 40])}).json()
            qs |= {h['title'] for h in j['query']['search']}
            off = j.get('continue', {}).get('sroffset')
    return qs


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    key = os.environ.get('TMDB_API_KEY', '').strip()
    if len(sys.argv) < 2 or not key:
        sys.exit(__doc__)
    old = json.loads(OUT.read_text()) if OUT.exists() else {}
    ids = imdb_ids(sys.argv[1:])
    print('عناوين IMDb:', len(ids))
    cols, tvs = tmdb(ids, key)
    print('سلاسل أفلام:', len(cols), '| مسلسلات:', len(tvs))
    qs = wikidata(ids)
    print('في Wikidata:', len(qs))
    snap = {'salt': old.get('salt') or secrets.token_hex(8), 'imdb': sorted(int(i[2:]) for i in ids),
            'col': sorted(cols), 'tv': sorted(tvs), 'q': sorted(int(q[1:]) for q in qs), 'bad': old.get('bad', [])}
    text = json.dumps(snap, separators=(',', ':'))
    OUT.write_text(text)
    print(f'كُتب {OUT} ({len(text)} حرفًا؛ حدّ السرّ 48 ألفًا)')


if __name__ == '__main__':
    main()
