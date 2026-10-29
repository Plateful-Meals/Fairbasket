#!/usr/bin/env python3
"""
Fairbasket: Aldi prices from Aldi's own product pages.

Aldi publishes a list of all its product pages (www.aldi.co.uk/sitemap_products.xml) and its robots.txt allows
product pages to be read (only its search pages are off limits, and this never uses them). Each product page shows
the name, the price, the pack size and the price per kg / litre / item.

What it does, in plain words
  1. Reads Aldi's product list once per run (one request).
  2. For each ingredient with rules in pipeline/config.json, picks the few product pages whose web address matches
     the ingredient's rules (the same "must" and "avoid" words used everywhere else), e.g. "semi skimmed milk".
  3. Reads those pages slowly, one at a time with a pause in between, saying who it is in the User-Agent.
     If Aldi ever refuses (403 / 429), it stops at once and keeps the old prices. It never tries to get round a block.
  4. Hands what it read to the usual matching rules (pipeline/update_prices.py), which pick the cheapest product of
     the right kind and pack size.
  5. Remembers which page was picked (aldi-map.json), so later runs only re-read those pages; once every 4 weeks it
     looks through all the candidate pages again in case something cheaper appeared.
  6. Same last check as the other sources (price_sources.guard): big jumps or prices far from the other shops are
     held back for a person to look at.

Usage
  python pipeline/aldi_prices.py --mode test --limit 20      report only, prices.json is not changed
  python pipeline/aldi_prices.py --mode live                 updates prices.json
  python pipeline/aldi_prices.py --fixtures DIR ...          read saved pages (DIR/sitemap.xml, DIR/pages/<n>.html)
"""
import argparse, datetime as dt, gzip, hashlib, html as htmllib, json, os, random, re, sys, tempfile, time
import urllib.error, urllib.request, urllib.robotparser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import price_sources as PS  # noqa: E402

SITE = 'https://www.aldi.co.uk'
SITEMAP = SITE + '/sitemap_products.xml'
UA = 'FairbasketPriceBot/1.0 (+https://fairbasket.co.uk/; contact@fairbasket.co.uk)'
MAP_FILE = os.path.join(HERE, 'aldi-map.json')
REMAP_DAYS = 28


class Refused(Exception):
    pass


class Web:
    def __init__(self, delay, fixtures=None):
        self.delay, self.fixtures, self.last, self.count = delay, fixtures, 0.0, 0

    def get(self, url):
        if self.fixtures:
            if url == SITEMAP:
                p = os.path.join(self.fixtures, 'sitemap.xml')
            else:
                p = os.path.join(self.fixtures, 'pages', hashlib.md5(url.encode()).hexdigest()[:12] + '.html')
            if not os.path.exists(p):
                return 404, ''
            self.count += 1
            return 200, open(p, encoding='utf-8').read()
        wait = self.last + self.delay + random.uniform(0, 0.5) - time.time()
        if wait > 0:
            time.sleep(wait)
        last_err = ''
        for attempt in range(3):
            self.last = time.time()
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'text/html,application/xml',
                                                       'Accept-Encoding': 'gzip', 'Accept-Language': 'en-GB'})
            try:
                with urllib.request.urlopen(req, timeout=40) as r:
                    data = r.read()
                    if r.headers.get('Content-Encoding') == 'gzip':
                        data = gzip.decompress(data)
                    self.count += 1
                    return r.status, data.decode('utf-8', 'replace')
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 429):
                    raise Refused(f'Aldi answered HTTP {e.code} for {url}')
                if e.code in (404, 410):
                    self.count += 1
                    return e.code, ''
                last_err = f'HTTP {e.code}'
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = str(e)[:80]
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(last_err)


def slug_words(url):
    s = url.rstrip('/').rsplit('/', 1)[-1]
    s = re.sub(r'-\d{9,}$', '', s)
    return s.replace('-', ' ')


def pick_candidates(matcher, U, pid, urls, k):
    r = matcher.rules(pid)
    scored = []
    for u in urls:
        t = slug_words(u)
        if any(not rx.search(t) for rx in r['must']):
            continue
        if any(rx.search(t) for _, rx in r['avoid']):
            continue
        if not r['frozen'] and matcher.frozen_rx.search(t):
            continue
        pref = 0 if (r['prefer'] and r['prefer'].search(t)) else 1
        scored.append((pref, len(t.split()), u))
    scored.sort()
    return [u for _, _, u in scored[:k]]


def parse_page(text, url):
    """-> item dict in the shape update_prices.normalise() reads, or None."""
    prod = None
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', text, re.S):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        for o in (d.get('@graph') if isinstance(d, dict) and d.get('@graph') else [d] if isinstance(d, dict) else d):
            if isinstance(o, dict) and o.get('@type') == 'Product':
                prod = o
                break
        if prod:
            break
    if not prod:
        return None
    offers = prod.get('offers') or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = offers.get('price')
    if price is None and isinstance(offers.get('priceSpecification'), dict):
        price = offers['priceSpecification'].get('price')
    brand = prod.get('brand')
    brand = brand.get('name') if isinstance(brand, dict) else brand
    item = {'name': htmllib.unescape(re.sub(r'\s+', ' ', str(prod.get('name') or '')).strip()),
            'brand': htmllib.unescape(str(brand or '')).title(), 'price': price, 'url': url}
    avail = str(offers.get('availability') or '')
    if avail:
        item['inStock'] = 'InStock' in avail or 'LimitedAvailability' in avail
    m = re.search(r'product-details__unit-of-measurement[^>]*>([^<]*)<', text)
    if m:
        uom = htmllib.unescape(m.group(1)).strip()            # "3.408 l (£0.70/1 L)"  or  "12 Each (£0.24/1 Each)"
        size = uom.split('(')[0].strip()
        size = re.sub(r'^(\d+)\s*each$', r'\1 pack', size, flags=re.I)
        item['size'] = size
        um = re.search(r'\(([^)]*)\)', uom)
        if um:
            item['pricePerUnit'] = um.group(1)
    m = re.search(r'base-price__regular[^>]*>(?:\s*<[^>]+>)*\s*([^<]*\d[^<]*)<', text)
    if m:
        item['originalPrice'] = m.group(1).strip()
    return item


def spread(ids, n):
    if n >= len(ids):
        return list(ids)
    step = len(ids) / n
    return [ids[int(i * step)] for i in range(n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['test', 'live'], default='test')
    ap.add_argument('--limit', type=int, default=20, help='test only: how many ingredients')
    ap.add_argument('--only')
    ap.add_argument('--remap', action='store_true', help='look through all candidate pages again for every ingredient')
    ap.add_argument('--candidates', type=int, default=8, help='product pages to read per ingredient when mapping')
    ap.add_argument('--delay', type=float, default=1.5, help='seconds between page visits')
    ap.add_argument('--max-pages', type=int, default=900)
    ap.add_argument('--fixtures')
    ap.add_argument('--html', default=os.path.join(ROOT, 'index.html'))
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--prices', default=os.path.join(ROOT, 'prices.json'))
    ap.add_argument('--report', default=os.path.join(HERE, 'aldi-report.md'))
    ap.add_argument('--map', default=MAP_FILE)
    ap.add_argument('--today')
    a = ap.parse_args()

    U = PS.load_updater()
    cfg = json.load(open(a.config, encoding='utf-8'))
    cat_all = U.read_catalogue(open(a.html, encoding='utf-8').read())
    tracked = [p for p in cat_all if p in cfg['products'] and not cfg['products'][p].get('skip') and 'aldi' not in cat_all[p]['miss']]
    cat = {p: cat_all[p] for p in cat_all if p in cfg['products']}
    today = a.today or PS.today_utc()
    t = dt.date.fromisoformat(today)
    names = PS.shop_names(cfg)
    matcher = U.Matcher(cfg, cat)

    if a.only:
        ids = [p for p in a.only.split(',') if p in tracked]
    elif a.mode == 'test':
        first = [p for p in cfg.get('test_products', []) if p in tracked]
        ids = (first + spread([p for p in tracked if p not in first], max(0, a.limit - len(first))))[:a.limit]
    else:
        ids = tracked
    web = Web(a.delay, a.fixtures)

    if not a.fixtures:
        rp = urllib.robotparser.RobotFileParser(SITE + '/robots.txt')
        rp.read()
        if not rp.can_fetch(UA, SITE + '/product/x-000000000000000001') or not rp.can_fetch(UA, SITEMAP):
            sys.exit('Aldi\'s robots.txt no longer allows product pages to be read, so nothing was done.')

    try:
        code, sm = web.get(SITEMAP)
    except (Refused, RuntimeError) as e:
        sys.exit(f'Could not read Aldi\'s product list ({e}). Nothing was changed.')
    urls = sorted(set(re.findall(r'<loc>\s*(https://www\.aldi\.co\.uk/product/[^<\s]+)\s*</loc>', sm)))
    if len(urls) < 500:
        sys.exit(f'Aldi\'s product list looks wrong ({len(urls)} products), so nothing was changed.')
    in_sitemap = set(urls)
    amap = json.load(open(a.map, encoding='utf-8')) if os.path.exists(a.map) else {}
    print(f'{a.mode} mode: {len(ids)} ingredients; Aldi lists {len(urls)} products.')

    plan, remapped, fresh_map = {}, 0, {}
    for pid in ids:
        m = amap.get(pid) or {}
        try:
            fresh = (t - dt.date.fromisoformat(m.get('mapped', ''))).days < REMAP_DAYS
        except ValueError:
            fresh = False
        if a.remap or not fresh or any(u not in in_sitemap for u in m.get('picked', [])):
            k = a.candidates + (4 if cfg['products'][pid].get('wide') else 0)
            plan[pid] = pick_candidates(matcher, U, pid, urls, k)
            fresh_map[pid] = {'cands': plan[pid], 'picked': [], 'mapped': today}
            remapped += 1
        else:
            plan[pid] = m.get('picked', [])          # only the pages picked last time (none if nothing matched)
    total = sum(len(v) for v in plan.values())
    print(f'Reading {total} product pages ({remapped} ingredients mapped afresh), {a.delay:g}s apart ...')

    work = tempfile.mkdtemp(prefix='aldi-')
    fx = os.path.join(work, 'fixtures', 'aldi')
    os.makedirs(fx, exist_ok=True)
    refused, errors, pages = '', [], 0
    for pid in ids:
        if not plan[pid]:
            continue                                  # nothing to read for this one until the next re-map
        rows = []
        for u in plan[pid]:
            if refused or pages >= a.max_pages:
                break
            try:
                code, body = web.get(u)
            except Refused as e:
                refused = str(e)
                break
            except RuntimeError as e:
                errors.append(f'{pid}: {u} ({e})')
                continue
            pages += 1
            if code != 200:
                continue
            it = parse_page(body, u)
            if it and it.get('price'):
                rows.append(it)
            else:
                errors.append(f'{pid}: could not read the price on {u}')
        with open(os.path.join(fx, pid + '.json'), 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False)
        if refused:
            break
    if refused:
        print('STOPPED: ' + refused + '. Old prices kept.')
    searched = [p for p in ids if os.path.exists(os.path.join(fx, p + '.json'))]
    if not searched:
        sys.exit('Nothing was read, so nothing was changed.')

    old = json.load(open(a.prices, encoding='utf-8')) if os.path.exists(a.prices) else {'prices': {}}
    out_prices = os.path.join(work, 'prices.json')
    code, log = PS.run_matcher(PS.matcher_config(cfg, ['aldi'], 'aldi-pages'), work, os.path.join(work, 'fixtures'), ['aldi'],
                               searched, a.html, a.prices, out_prices, a.report, today, 'live')
    print('\n'.join(log.strip().splitlines()[-4:]))
    if code != 0:
        sys.exit('The matching step failed (see above). prices.json was not changed.')
    new = json.load(open(out_prices, encoding='utf-8'))
    held = PS.guard(old, new, ['aldi'], today, names)
    PS.keep_recent_packs(old, new, ['aldi'], today)
    PS.tag_source(new, old, ['aldi'], today, 'aldi-pages')
    held += PS.respect_priority(old, new, ['aldi'], today, names)
    PS.expire(new, today)
    for pid in searched:                                      # remember which pages were picked
        if pid in fresh_map:
            amap[pid] = fresh_map[pid]                        # (an ingredient not read because of a stop keeps its old map)
        e = new.get('prices', {}).get(pid, {}).get('aldi')
        if e and e.get('checked') == today and pid in amap:
            amap[pid]['picked'] = [it['u'] for it in e.get('items', []) if it.get('u')]
    if a.mode == 'live':
        PS.finish(U, cat_all, a.prices, new)
        with open(a.map, 'w', encoding='utf-8') as f:
            json.dump(dict(sorted(amap.items())), f, ensure_ascii=False, indent=1)

    rep = open(a.report, encoding='utf-8').read().split('\n')
    head = [f'# Fairbasket Aldi update: {a.mode} run, {today}', '',
            '_Test run: prices.json was NOT changed. The lines below show what a live run would do._' if a.mode == 'test' else '', '',
            f'Ingredients: {len(ids)}. Aldi product pages read: {pages} (one every {a.delay:g}s or more). '
            f'Ingredients mapped afresh: {remapped}.' + (f' **Stopped early: {refused}.**' if refused else ''), '']
    if errors:
        head += ['## Pages that could not be read', ''] + [f'- {e}' for e in errors[:15]] + ['']
    body = [ln for ln in rep[1:] if not ln.startswith('Ingredients searched:')]
    with open(a.report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(head + held + body))
    print(f'Report written to {a.report}. Pages read: {pages}.' + (' Test run: prices.json was NOT changed.' if a.mode == 'test' else ''))
    if refused and a.mode == 'live':
        sys.exit(0)


if __name__ == '__main__':
    main()
