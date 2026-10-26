#!/usr/bin/env python3
"""
Plateful weekly price updater.

What it does, in plain words
  1. For every ingredient in the site's catalogue it asks Apify to search a shop's website (Aldi, Lidl, Morrisons).
  2. It throws away anything that is not the plain everyday product (organic, flavoured, ready meals, wrong size...).
  3. For each pack size it keeps the cheapest matching product (by price per gram / per item).
  4. It merges the result into prices.json (the file the website reads). Anything it could not refresh is left as it was.
  5. It writes pipeline/last-run-report.md so you can see exactly what it picked and why things were rejected.

Modes
  --mode test   a handful of ingredients, costs a few cents, never touches prices.json (report + raw samples only)
  --mode live   all ingredients, updates prices.json

Needs only Python 3.9+ (standard library). The Apify token is read from the APIFY_TOKEN environment variable
and is never printed or written anywhere.
"""
import argparse, copy, datetime as dt, json, os, re, sys, threading, time, urllib.error, urllib.request, zlib
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SHOP_ORDER = ['tesco', 'asda', 'sainsburys', 'morrisons', 'aldi', 'lidl', 'waitrose', 'iceland']
API = 'https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items'


# ----------------------------------------------------------------------------- catalogue (read from index.html)
def read_catalogue(html):
    """id -> {name, unit, packs:[[size, price]], miss:[shop ids]} from the P = {...} block of the site."""
    cat = {}
    head = re.compile(r"^\s{2}([a-z_]+):\{n:'((?:[^'\\]|\\.)*)',u:'(\w+)',", re.M)
    for m in head.finditer(html):
        depth, e = 1, m.end()
        while depth:
            ch = html[e]
            depth += (ch == '{') - (ch == '}')
            e += 1
        body = html[m.end():e]
        k = body.index('packs:') + len('packs:')
        depth, j = 0, k
        while True:
            ch = body[j]
            depth += (ch == '[') - (ch == ']')
            j += 1
            if depth == 0:
                break
        packs = [[float(a) if '.' in a else int(a), float(b)] for a, b in re.findall(r'\[(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)', body[k:j])]
        miss = re.search(r'miss:\[([^\]]*)\]', body)
        cat[m.group(1)] = {'name': m.group(2).replace("\\'", "'"), 'unit': m.group(3), 'packs': packs,
                           'miss': re.findall(r"'([a-z]+)'", miss.group(1)) if miss else []}
    return cat


def read_live_block(html):
    m = re.search(r'/\*LIVE-DATA-START\*/const LIVE = (\{.*?\});/\*LIVE-DATA-END\*/', html, re.S)
    return json.loads(m.group(1)) if m else {'checked': '', 'label': '', 'prices': {}}


# ----------------------------------------------------------------------------- reading what the actors return
NAME_KEYS = ['name', 'title', 'productName', 'product_name', 'fullTitle', 'productTitle', 'label', 'description']
PRICE_KEYS = ['price', 'currentPrice', 'current_price', 'priceValue', 'sellingPrice', 'nowPrice', 'salePrice', 'amount']
WAS_KEYS = ['originalPrice', 'original_price', 'oldPrice', 'old_price', 'wasPrice', 'was_price', 'regularPrice',
            'regular_price', 'strikethroughPrice', 'strikePrice', 'previousPrice', 'priceBefore']
UNIT_KEYS = ['pricePerUnit', 'price_per_unit', 'unitPrice', 'unit_price', 'basePrice', 'base_price',
             'referencePrice', 'comparisonPrice', 'pricePerKg']
SIZE_KEYS = ['packSize', 'pack_size', 'sellingSize', 'selling_size', 'size', 'quantity', 'packaging',
             'netContent', 'contentSize', 'weight', 'volume']
URL_KEYS = ['url', 'link', 'productUrl', 'product_url', 'canonicalUrl', 'href', 'productLink']
BRAND_KEYS = ['brand', 'brandName', 'manufacturer']
PROMO_KEYS = ['promotion', 'promo', 'offer', 'promotionText', 'badge']
MEMBER_KEYS = ['memberPrice', 'clubcardPrice', 'nectarPrice', 'loyaltyPrice', 'cardPrice', 'lidlPlusPrice']


def pick(obj, names, depth=3):
    """Find the first of `names` (case-insensitive, in priority order) in obj or in objects nested inside it."""
    want = [n.lower() for n in names]
    level = [obj]
    for _ in range(depth + 1):
        nxt = []
        for node in level:
            if not isinstance(node, dict):
                continue
            low = {str(k).lower(): v for k, v in node.items()}
            for n in want:
                if n in low and low[n] not in (None, '', [], {}):
                    return low[n]
            nxt.extend(v for v in node.values() if isinstance(v, dict))
        level = nxt
    return None


def parse_money(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ('value', 'amount', 'price', 'now', 'current', 'formatted', 'text'):
            if k in v:
                r = parse_money(v[k])
                if r is not None:
                    return r
        return None
    m = re.search(r'(£)?\s*(\d+(?:[.,]\d+)?)\s*(p\b)?', str(v))
    if not m:
        return None
    x = float(m.group(2).replace(',', '.'))
    return x / 100 if (m.group(3) and not m.group(1)) else x


def normalise(raw):
    name = pick(raw, NAME_KEYS)
    if isinstance(name, dict):
        name = pick(name, ['text', 'value', 'name'])
    brand = pick(raw, BRAND_KEYS)
    if isinstance(brand, dict):
        brand = pick(brand, ['name', 'text'])
    size = pick(raw, SIZE_KEYS)
    unit = pick(raw, UNIT_KEYS)
    promo = pick(raw, PROMO_KEYS)
    url = pick(raw, URL_KEYS)
    stock = pick(raw, ['inStock', 'in_stock', 'available'])
    return {
        'name': str(name).strip()[:160] if name else '',
        'brand': str(brand).strip() if isinstance(brand, (str, int, float)) else '',
        'price': parse_money(pick(raw, PRICE_KEYS)),
        'was': parse_money(pick(raw, WAS_KEYS)),
        'member': parse_money(pick(raw, MEMBER_KEYS)),
        'size_text': str(size) if isinstance(size, (str, int, float)) and not isinstance(size, bool) else '',
        'unit_text': str(unit) if isinstance(unit, str) else '',
        'promo': str(promo)[:80] if isinstance(promo, str) else '',
        'url': str(url) if isinstance(url, str) and url.startswith('https://') else '',
        'in_stock': stock if isinstance(stock, bool) else None,
    }


# ----------------------------------------------------------------------------- sizes
G = {'g': 1.0, 'kg': 1000.0}
ML = {'ml': 1.0, 'cl': 10.0, 'l': 1000.0, 'ltr': 1000.0, 'ltrs': 1000.0, 'litre': 1000.0, 'litres': 1000.0,
      'liter': 1000.0, 'liters': 1000.0, 'pint': 568.26, 'pints': 568.26, 'pt': 568.26}
UNITS = r'kg|g|ml|cl|ltrs?|litres?|liters?|l|pints?|pt'
MULT_RE = re.compile(r'(\d+)\s*(?:x|×|\*)\s*(\d+(?:\.\d+)?)\s*(' + UNITS + r')\b', re.I)
QTY_RE = re.compile(r'(?<![\w.])(\d+(?:\.\d+)?)\s*(' + UNITS + r')\b', re.I)
COUNT_RES = [
    re.compile(r'\bpack of (\d+)\b', re.I),
    re.compile(r'\b(\d+)\s*(?:pack|pk|pcs?|pieces?|count|ct)\b', re.I),
    re.compile(r'\b(\d+)s\b', re.I),
    re.compile(r'(?<![\w.])(?:x|×)\s?(\d+)\b(?!\s*(?:' + UNITS + r')\b)', re.I),
    re.compile(r'\b(\d+)\s*(?:x|×)(?!\s*\d)', re.I),
    re.compile(r'\b(\d+)\s+(?:[a-z-]+\s+){0,3}'
               r'(?:eggs?|rolls?|buns?|wraps?|tortillas?|sausages?|pittas?|pitta breads?|fish fingers?|hash browns?|noodles?|'
               r'onions?|peppers?|tomatoes|lemons?|bananas?|courgettes?|potatoes|cubes?|bulbs?|lettuces?|'
               r'burgers?|steaks?|rashers?|fillets?|bagels?|naans?|pizza bases|bases|avocados?|apples?|oranges?|chillies|chilis?|'
               r'leeks?|aubergines?|baguettes?|cucumbers?|cauliflowers?)\b', re.I),
]


def _unit_factor(table, u):
    return table.get(u.lower())


def parse_sizes(text):
    """All sizes found in a piece of text: {'g': grams, 'ml': millilitres, 'count': n} (only those present)."""
    out = {}
    if not text:
        return out
    t = str(text)
    m = MULT_RE.search(t)
    if m:
        n, amt, u = int(m.group(1)), float(m.group(2)), m.group(3)
        if _unit_factor(G, u):
            out['g'] = n * amt * G[u.lower()]
        elif _unit_factor(ML, u):
            out['ml'] = n * amt * ML[u.lower()]
    else:
        for m in QTY_RE.finditer(t):
            amt, u = float(m.group(1)), m.group(2).lower()
            if u in G and 'g' not in out:
                out['g'] = amt * G[u]
            elif u in ML and 'ml' not in out:
                out['ml'] = amt * ML[u]
    for rx in COUNT_RES:
        m = rx.search(t)
        if m and 1 <= int(m.group(1)) <= 200:
            out['count'] = int(m.group(1))
            break
    return out


def parse_unit_price(text):
    """'£0.30/100g' -> (0.30, 100, 'g'); '£1.10 per litre' -> (1.10, 1, 'ml'*1000 handled by caller). Returns (price, n, unit)."""
    if not text:
        return None
    m = re.search(r'(£)?\s*(\d+(?:\.\d+)?)\s*(p\b)?\s*(?:/|per|a)\s*(\d+(?:\.\d+)?)?\s*(kg|g|ml|cl|ltr|litres?|liters?|l|each|ea|item|unit|100g|100ml)\b',
                  text, re.I)
    if not m:
        return None
    price = float(m.group(2))
    if m.group(3) and not m.group(1):
        price /= 100
    n = float(m.group(4)) if m.group(4) else 1.0
    u = m.group(5).lower()
    if u == '100g':
        n, u = 100.0, 'g'
    if u == '100ml':
        n, u = 100.0, 'ml'
    return price, n, u


def item_quantities(it):
    """Everything we can work out about an item's pack size, most trustworthy source first."""
    q = {}
    for src in (it['size_text'], it['name']):
        for k, v in parse_sizes(src).items():
            q.setdefault(k, v)
    if not q:
        up = parse_unit_price(it['unit_text'])
        if up and up[0] > 0 and it['price']:
            per, n, u = up
            amount = it['price'] / per * n
            if u in G:
                q['g'] = amount * G[u]
            elif u in ML:
                q['ml'] = amount * ML[u]
            elif u in ('each', 'ea', 'item', 'unit'):
                q['count'] = round(amount)
                q['unitprice_each'] = True
    return q


def per_each(it):
    up = parse_unit_price(it['unit_text'])
    return bool(up and up[2] in ('each', 'ea', 'item', 'unit'))


# ----------------------------------------------------------------------------- which ingredients are searched this week
def week_number(day):
    """A counter that goes up by exactly 1 every 7 days (so "every 2 weeks" really is every 2 weeks, even across new year)."""
    return day.toordinal() // 7


def is_due(pid, pc, day):
    """refresh = N in config.json means "search this ingredient every N weeks". Each ingredient gets its own fixed offset
    (from its id) so the ingredients are spread evenly over the weeks instead of all landing in the same one."""
    n = int(pc.get('refresh') or 1)
    return n <= 1 or (week_number(day) + zlib.crc32(pid.encode('utf-8'))) % n == 0


# ----------------------------------------------------------------------------- choosing the cheapest matching product
class Matcher:
    def __init__(self, cfg, cat):
        self.cfg, self.cat = cfg, cat
        self.frozen_rx = re.compile(cfg['frozen_regex'], re.I)
        self.tags = {k: re.compile(v, re.I) for k, v in cfg['tags'].items()}
        self.cache = {}

    def rules(self, pid):
        if pid in self.cache:
            return self.cache[pid]
        pc = self.cfg['products'][pid]
        avoid = [(f'"{t}" words', rx) for t, rx in self.tags.items() if t not in pc.get('allow', [])]
        if pc.get('avoid'):
            avoid.append(('unwanted words', re.compile(pc['avoid'], re.I)))
        r = {
            'must': [re.compile(x, re.I) for x in pc.get('must', [])],
            'avoid': avoid,
            'prefer': re.compile(pc['prefer'], re.I) if pc.get('prefer') else None,
            'frozen': bool(pc.get('frozen')), 'loose': bool(pc.get('loose')),
            'mult': pc.get('mult', 1), 'tol': pc.get('tol', self.cfg['tolerance']),
            'alt': pc.get('alt', {}),
            'count_rx': re.compile(r'(?<![\w.])(\d+)\s+(?:[a-z-]+\s+){0,3}(?:' + pc['count_nouns'] + r')\b', re.I) if pc.get('count_nouns') else None,
        }
        self.cache[pid] = r
        return r

    def slot_for(self, pid, it, q, rules):
        """Which catalogue pack slot does this item fill, and what is its real size? -> (slot_index, actual_size) or (None, reason)."""
        cp = self.cat[pid]
        unit = cp['unit']
        tol = rules['tol']
        best = None
        for i, (slot, _price) in enumerate(cp['packs']):
            actual = None
            if unit == 'g' and 'g' in q:
                actual = q['g']
            elif unit == 'ml' and 'ml' in q:
                actual = q['ml']
            elif unit == 'each':
                if 'count' in q:
                    actual = q['count']
                elif rules['loose'] and slot == 1 and per_each(it) and not q:
                    actual = 1
                alt = rules['alt'].get(str(slot))
                if actual is None and alt and alt[0] in q and alt[1]:
                    actual = slot * q[alt[0]] / alt[1]
            elif unit == 'clove':
                n = q.get('count', 1 if (rules['loose'] and per_each(it) and not q) else None)
                actual = n * rules['mult'] if n else None
            if actual is None:
                continue
            diff = abs(actual - slot) / slot
            if diff <= tol and (best is None or diff < best[0]):
                best = (diff, i, actual)
        if best is None:
            return None, ('no size found' if not q else 'size not one we compare')
        _, i, actual = best
        slot = cp['packs'][i][0]
        if abs(actual - slot) / slot <= 0.015:
            actual = slot
        elif unit in ('g', 'ml'):
            actual = int(round(actual))
        elif unit == 'each':
            actual = round(actual, 2) if actual != int(actual) else int(actual)
        else:
            actual = int(round(actual))
        return i, actual

    def choose(self, pid, items):
        """items: normalised items for one search. -> (picks, trace). picks: {slot_index: pick dict}."""
        cp, rules = self.cat[pid], self.rules(pid)
        cfg = self.cfg
        trace = []          # (how far it got, name, price, reason) for things rejected
        cands = {}          # slot -> [candidate]
        for it in items:
            nm = (it['brand'] + ' ' + it['name']).strip() if it['brand'].lower() not in it['name'].lower() else it['name']
            low = nm.lower()
            def no(reason, rank=0):
                trace.append((rank, nm[:70], it['price'], reason))
            if not it['name'] or not it['price'] or it['price'] <= 0:
                no('no price', 0); continue
            if it['in_stock'] is False:
                no('out of stock', 1); continue
            if any(not rx.search(low) for rx in rules['must']):
                no('not the right product', 2); continue
            hit = next(((lbl, rx.search(low).group(0)) for lbl, rx in rules['avoid'] if rx.search(low)), None)
            if hit:
                no(f'{hit[0]}: "{hit[1]}"', 4); continue
            if not rules['frozen'] and (self.frozen_rx.search(low) or self.frozen_rx.search(it['url'])):
                no('frozen', 4); continue
            q = item_quantities(it)
            if rules['count_rx'] and 'count' not in q:
                m = rules['count_rx'].search(it['name'])
                if m and 1 <= int(m.group(1)) <= 200:
                    q['count'] = int(m.group(1))
            slot, actual = self.slot_for(pid, it, q, rules)
            if slot is None:
                no(actual, 5); continue
            slot_size, base_price = cp['packs'][slot]
            price, was = it['price'], it['was']
            expect = base_price * actual / slot_size
            lo, hi = cfg['sanity_low'] * expect, cfg['sanity_high'] * expect
            if not (lo <= price <= hi):
                if float(price).is_integer() and price >= 50 and lo <= price / 100 <= hi:
                    price = price / 100                      # the actor gave pence
                    was = was / 100 if was else was
                else:
                    no(f'price £{price:.2f} looks wrong for this pack', 6); continue
            sale = None
            if was and was > price and was <= price * 3 and lo <= was <= hi * 1.5:
                sale = price
                price = was
            mem = it.get('member')
            card = mem if (mem and mem < (sale if sale is not None else price) and mem >= 0.4 * price) else None
            cands.setdefault(slot, []).append({
                'slot': slot, 'size': actual, 'price': price, 'sale': sale, 'card': card, 'name': nm[:90], 'url': it['url'],
                'promo': it['promo'], 'unit': (sale if sale is not None else price) / actual,
                'preferred': bool(rules['prefer'] and rules['prefer'].search(low)),
            })
        picks = {}
        for slot, cs in cands.items():
            pref = [c for c in cs if c['preferred']]
            pool = pref or cs
            picks[slot] = min(pool, key=lambda c: (c['unit'], c['price']))
            picks[slot]['others'] = len(cs) - 1
        return picks, trace


def build_entry(picks, today):
    order = sorted(picks)
    packs, items = [], []
    for s in order:
        p = picks[s]
        # a pack is [size, price], [size, price, salePrice] or [size, price, salePrice-or-null, loyaltyCardPrice]
        pk = [p['size'], round(p['price'], 2)] + ([round(p['sale'], 2)] if p['sale'] is not None else [])
        if p.get('card') is not None:
            pk = pk[:2] + [pk[2] if len(pk) > 2 else None, round(p['card'], 2)]
        packs.append(pk)
        items.append({'n': p['name']} | ({'u': p['url']} if p['url'] else {}))
    e = {'packs': packs, 'product': items[0]['n'], 'checked': today, 'items': items}
    if any(picks[s]['sale'] is not None for s in order):
        e['saleNote'] = next((picks[s]['promo'] for s in order if picks[s]['sale'] is not None and picks[s]['promo']), '')
        if not e['saleNote']:
            del e['saleNote']
    return e


# ----------------------------------------------------------------------------- talking to Apify
class Fatal(Exception):
    pass


class ShopDown(Exception):
    pass


def apify_search(actor, body, token, timeout_s, max_items, max_charge, tries=3):
    url = API.format(actor=actor) + f'?timeout={int(timeout_s)}&maxItems={int(max_items)}&maxTotalChargeUsd={max_charge}&clean=true'
    data = json.dumps(body).encode()
    last = ''
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method='POST', headers={
            'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token, 'User-Agent': 'plateful-price-updater'})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s + 60) as r:
                out = json.loads(r.read().decode('utf-8', 'replace') or '[]')
                return out if isinstance(out, list) else []
        except urllib.error.HTTPError as e:
            text = e.read().decode('utf-8', 'replace')[:900].replace(token, '***')
            last = f'HTTP {e.code} {text}'
            if e.code in (401, 403):
                raise Fatal('Apify refused the token or the actor (HTTP %d). Check the APIFY_TOKEN secret. %s' % (e.code, text))
            if e.code == 402:
                raise Fatal('Apify says the account is out of credit (HTTP 402). Top up or upgrade the plan. Apify said: ' + text + '')
            if e.code in (400, 404):
                raise ShopDown(last)
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            last = f'{type(e).__name__}: {e}'
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(last)


def fixture_search(fixtures, shop, pid):
    path = os.path.join(fixtures, shop, pid + '.json')
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def fill(tpl, kw, n):
    if isinstance(tpl, dict):
        return {k: fill(v, kw, n) for k, v in tpl.items()}
    if tpl == '{n}':
        return n
    if isinstance(tpl, str):
        return tpl.replace('{kw}', kw)
    return tpl


# ----------------------------------------------------------------------------- output files
def dump_prices(d, cat):
    order = [p for p in cat if p in d['prices']] + [p for p in d['prices'] if p not in cat]
    out = ['{']
    for k in d:
        if k != 'prices':
            out.append(f' {json.dumps(k)}: {json.dumps(d[k], ensure_ascii=False, separators=(",", ":"))},')
    out.append(' "prices": {')
    for i, pid in enumerate(order):
        shops = sorted(d['prices'][pid], key=lambda s: SHOP_ORDER.index(s) if s in SHOP_ORDER else 99)
        out.append(f'  {json.dumps(pid)}: {{')
        for j, s in enumerate(shops):
            out.append('   ' + json.dumps(s) + ': ' + json.dumps(d['prices'][pid][s], ensure_ascii=False, separators=(',', ':'))
                       + (',' if j < len(shops) - 1 else ''))
        out.append('  }' + (',' if i < len(order) - 1 else ''))
    out += [' }', '}']
    text = '\n'.join(out) + '\n'
    json.loads(text)        # must round-trip
    return text


def gbp(x):
    return '£%.2f' % x


def main():
    ap = argparse.ArgumentParser(description='Plateful weekly price updater')
    ap.add_argument('--mode', choices=['test', 'live'], default='test')
    ap.add_argument('--shops', default='aldi,lidl,morrisons')
    ap.add_argument('--source', choices=['apify', 'browser'], default='apify',
                    help='apify = Apify robots (the GitHub job).  browser = Plateful\'s own checker in a real browser on your computer (check_prices.py)')
    ap.add_argument('--hidden', action='store_true', help='browser source only: run the browser without a window')
    ap.add_argument('--profile', default=os.path.join(ROOT, '.browser-profile'), help='browser source only: where the browser keeps its profile')
    ap.add_argument('--only', help='comma-separated ingredient ids (overrides the test/live selection)')
    ap.add_argument('--fixtures', help='read saved actor output from DIR/<shop>/<id>.json instead of calling Apify (for tests)')
    ap.add_argument('--html', default=os.path.join(ROOT, 'index.html'))
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--prices', default=os.path.join(ROOT, 'prices.json'))
    ap.add_argument('--report', default=os.path.join(HERE, 'last-run-report.md'))
    ap.add_argument('--samples-dir', default=os.path.join(HERE, 'samples'))
    ap.add_argument('--today', help='YYYY-MM-DD (default: today, UTC)')
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--scope', choices=['all', 'due', 'everyday'], default='all',
                    help='live mode only. all = search every ingredient. due = only the ingredients whose "refresh" turn it is this week (what the Monday run uses). '
                         'everyday = only the everyday ingredients (refresh 1), what the extra daily / every-2-days runs use')
    ap.add_argument('--freq', default='week',
                    help='how often everyday items are refreshed, for the note on the site: week, day or 2days')
    ap.add_argument('--max-spend', type=float, default=12.0, help='stop launching searches once the estimated spend passes this many US dollars')
    ap.add_argument('--dry-run', action='store_true', help='live mode, but do not write prices.json')
    ap.add_argument('--check-config', action='store_true')
    ap.add_argument('--estimate', action='store_true', help='print the estimated cost and exit')
    a = ap.parse_args()

    cfg = json.load(open(a.config, encoding='utf-8'))
    html = open(a.html, encoding='utf-8').read()
    cat_all = read_catalogue(html)
    # Only the products that have rules in config.json are checked on the shop websites.
    # Every other product in the site keeps its estimated price (add a rules block here to start tracking it).
    cat = {pid: c for pid, c in cat_all.items() if pid in cfg['products']}
    untracked = len(cat_all) - len(cat)
    matcher = Matcher(cfg, cat)

    problems = []
    for pid in cfg['products']:
        if pid not in cat_all:
            problems.append(f'{pid}: in config.json but not in the site')
    for pid, pc in cfg['products'].items():
        try:
            r = matcher.rules(pid)
        except re.error as e:
            problems.append(f'{pid}: bad pattern ({e})')
            continue
        # A pattern that can match the empty string (a stray "|" at an end, or "||") matches EVERYTHING,
        # so every product would be rejected (an "avoid") or accepted (a "must"). Catch it here.
        for label, rx in [('must', x) for x in r['must']] + [('avoid', x) for _, x in r['avoid']] + \
                         [('prefer', r['prefer'])] + [('count_nouns', re.compile(pc['count_nouns'], re.I) if pc.get('count_nouns') else None)]:
            if rx is not None and rx.search(''):
                problems.append(f'{pid}: a "{label}" pattern matches the empty string (check for a stray "|"): {rx.pattern[:60]}')
    if a.check_config:
        if problems:
            print('\n'.join(problems)); sys.exit(1)
        print(f'config.json is consistent with the site: {len(cat)} ingredients are checked on the shop websites, '
              f'{untracked} more products use estimated prices, all patterns compile.')
        return
    if problems:
        print('Config problems:\n' + '\n'.join(problems)); sys.exit(1)

    today = a.today or dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')
    today_d = dt.date.fromisoformat(today)
    label = f'{today_d.day} {today_d.strftime("%b %Y")}'
    shops = [s for s in a.shops.split(',') if s]
    for s in shops:
        if s not in cfg['shops']:
            sys.exit(f'Unknown shop "{s}". Known: {", ".join(cfg["shops"])}')
        if a.source == 'browser' and not cfg['shops'][s].get('browser'):
            sys.exit(f'{cfg["shops"][s]["name"]} cannot be checked by the home checker (its website does not allow automated visits). '
                     f'Shops it can check: {", ".join(k for k, v in cfg["shops"].items() if v.get("browser"))}')
        if a.source == 'apify' and not cfg['shops'][s].get('actor'):
            sys.exit(f'{cfg["shops"][s]["name"]} has no Apify robot. Use the home checker (check_prices.py) for it.')
    due_note = ''
    if a.only:
        pids = [p for p in a.only.split(',') if p]
    elif a.mode == 'test':
        pids = list(cfg['test_products'])
    elif a.scope == 'due':
        pids = [p for p in cat if is_due(p, cfg['products'][p], today_d)]
        due_note = f' ({len(pids)} of {len(cat)} are due this week; the rest keep the price they have)'
    elif a.scope == 'everyday':
        pids = [p for p in cat if int(cfg['products'][p].get('refresh') or 1) <= 1]
        due_note = f' ({len(pids)} everyday ingredients; the rest keep the price they have)'
    else:
        pids = list(cat)
    bad = [p for p in pids if p not in cat]
    if bad:
        sys.exit('Unknown ingredient id(s): ' + ', '.join(bad) +
                 ('  (' + ', '.join(p for p in bad if p in cat_all) + ' is in the site but has no rules in config.json, so it is not checked)'
                  if any(p in cat_all for p in bad) else ''))

    def plan(ids):
        tasks, skipped = [], []
        for pid in ids:
            pc = cfg['products'][pid]
            if pc.get('skip'):
                skipped.append((pid, pc['skip'])); continue
            for s in shops:
                if s in cat[pid]['miss']:
                    continue
                n = cfg['shops'][s]['n_wide'] if pc.get('wide') else cfg['shops'][s]['n']
                tasks.append((s, pid, n))
        return tasks, skipped

    def cost(tasks, per_week=False):
        tot = 0.0
        for s, pid, n in tasks:
            c = n * cfg['shops'][s].get('per_1000_results', 0) / 1000
            c = 0 if a.source == 'browser' else c
            tot += c / max(1, int(cfg['products'][pid].get('refresh') or 1)) if per_week else c
        return tot

    tasks, skipped = plan(pids)
    est = cost(tasks)
    print(f'{a.mode} mode: {len(pids)} ingredients{due_note}, {len(tasks)} searches, estimated worst-case cost about ${est:.2f}')
    if a.estimate:
        all_tasks, _ = plan(list(cat))
        due_tasks, _ = plan([p for p in cat if is_due(p, cfg['products'][p], today_d)])
        avg = cost(all_tasks, per_week=True)
        print(f'  every ingredient, every search ("--scope all"):  {len(all_tasks)} searches, about ${cost(all_tasks):.2f}')
        print(f'  only the ingredients due this week ("--scope due", {today}): {len(due_tasks)} searches, about ${cost(due_tasks):.2f}')
        ev_tasks, _ = plan([p for p in cat if int(cfg['products'][p].get('refresh') or 1) <= 1])
        ev = cost(ev_tasks)
        print(f'  only the everyday ingredients ("--scope everyday"): {len(ev_tasks)} searches, about ${ev:.2f} a run')
        print(f'  Monday run plus everyday runs on the other days: every 2 days about ${(avg + 3 * ev) * 52 / 12:.2f} a month, every day about ${(avg + 6 * ev) * 52 / 12:.2f} a month')
        print(f'  average week once the refresh settings have spread things out: about ${avg:.2f}, so roughly ${avg * 52 / 12:.2f} a month')
        print('  (worst case: Apify bills per result returned, so real runs usually cost less)')
        return

    token = os.environ.get('APIFY_TOKEN', '')
    if not a.fixtures and not token and a.source == 'apify':
        sys.exit('APIFY_TOKEN is not set. In GitHub: Settings > Secrets and variables > Actions > New repository secret.')

    # ---- run the searches
    lock = threading.Lock()
    st = {s: {'ok': 0, 'err': 0, 'empty': 0, 'items': 0, 'dead': '', 'errors': []} for s in shops}
    spent = {'usd': 0.0}
    fatal = {'msg': ''}
    results = {}          # (shop, pid) -> list of normalised items, or None on failure
    raws = {}

    def work(task):
        s, pid, n = task
        shop = cfg['shops'][s]
        with lock:
            if fatal['msg'] or st[s]['dead']:
                return
            per = 0 if a.source == 'browser' else shop.get('per_1000_results', 0)
            if spent['usd'] + n * per / 1000 > a.max_spend:
                st[s]['errors'].append(f'{pid}: skipped, spending limit ${a.max_spend:.2f} reached')
                return
            spent['usd'] += n * per / 1000
        kw = cfg['products'][pid]['kw']
        try:
            if a.fixtures:
                raw = fixture_search(a.fixtures, s, pid)
            elif a.source == 'browser':
                try:
                    raw = BROWSER['b'].search(s, kw, n)
                except BROWSER['refused'] as e:
                    raise ShopDown(str(e))
                except Exception as e:                      # page did not load, timed out...
                    raise RuntimeError(str(e).splitlines()[0][:150])
            else:
                body = fill(shop['input'], kw, n)
                charge = round(max(0.5, n * shop['per_1000_results'] / 1000 * 2), 3)
                raw = apify_search(shop['actor'], body, token, 240, n, charge)
        except Fatal as e:
            with lock:
                fatal['msg'] = str(e)
            return
        except (ShopDown, RuntimeError) as e:
            with lock:
                st[s]['err'] += 1
                st[s]['errors'].append(f'{pid}: {e}')
                if st[s]['ok'] == 0 and st[s]['err'] >= 3:
                    st[s]['dead'] = 'the first 3 searches all failed, so the rest were not attempted'
                if a.source == 'browser' and isinstance(e, ShopDown):
                    st[s]['dead'] = f'the website refused a visit ({e}), so this shop was stopped for the rest of the run and its old prices kept'
            print(f'[{s}] {pid}: FAILED {str(e)[:600]}')
            return
        items = [normalise(r) for r in raw if isinstance(r, dict)]
        with lock:
            st[s]['ok'] += 1
            st[s]['items'] += len(items)
            st[s]['empty'] += (not items)
            results[(s, pid)] = items
            raws[(s, pid)] = raw[:6]
        print(f'[{s}] {pid}: {len(items)} results')

    BROWSER = {}
    if a.source == 'browser' and not a.fixtures:
        # one real browser, one page at a time; the shops take turns so each one is visited only every few seconds
        from browser_search import BrowserShops, ShopRefused
        BROWSER['refused'] = ShopRefused
        by_shop = {}
        for t in tasks:
            by_shop.setdefault(t[0], []).append(t)
        order = []
        while any(by_shop.values()):
            for sh in list(by_shop):
                if by_shop[sh]:
                    order.append(by_shop[sh].pop(0))
        with BrowserShops(cfg['shops'], a.profile, hidden=a.hidden) as bs:
            BROWSER['b'] = bs
            for t in order:
                work(t)
    else:
        BROWSER['refused'] = ShopDown
        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            list(ex.map(work, tasks))
    if fatal['msg']:
        print('STOPPED: ' + fatal['msg'])

    # ---- decide which shops are trustworthy this run
    shop_ok = {}
    for s in shops:
        ran = st[s]['ok'] + st[s]['err']
        hit = (st[s]['ok'] - st[s]['empty']) / ran if ran else 0
        if st[s]['dead']:
            shop_ok[s] = False
        elif ran == 0:
            shop_ok[s] = False
            st[s]['dead'] = 'nothing was searched'
        elif hit < cfg['shop_min_hit_ratio']:
            shop_ok[s] = False
            st[s]['dead'] = f'only {hit:.0%} of searches returned anything (needs {cfg["shop_min_hit_ratio"]:.0%}), so the actor looks broken and its results were ignored'
        else:
            shop_ok[s] = True

    # ---- pick products
    picked, unmatched, traces = {}, [], {}
    for (s, pid), items in sorted(results.items(), key=lambda kv: (list(cat).index(kv[0][1]), kv[0][0])):
        picks, trace = matcher.choose(pid, items)
        traces[(s, pid)] = trace
        if picks:
            picked[(s, pid)] = picks
        else:
            unmatched.append((s, pid, len(items)))

    # ---- merge into prices.json
    old = {'checked': '', 'label': '', 'prices': {}}
    if os.path.exists(a.prices):
        old = json.load(open(a.prices, encoding='utf-8'))
    else:
        old = read_live_block(html)
    new = copy.deepcopy(old)
    new.setdefault('prices', {})
    changes, dropped, kept = [], [], 0
    for (s, pid), picks in picked.items():
        if not shop_ok[s]:
            continue
        entry = build_entry(picks, today)
        prev = new['prices'].get(pid, {}).get(s)
        new['prices'].setdefault(pid, {})[s] = entry
        changes.append((s, pid, prev, entry))
    for (s, pid), items in results.items():
        if not shop_ok[s] or (s, pid) in picked:
            continue
        prev = new['prices'].get(pid, {}).get(s)
        if prev:
            try:
                age = (today_d - dt.date.fromisoformat(prev.get('checked') or old.get('checked') or today)).days
            except ValueError:
                age = 0
            if age > cfg['stale_days']:
                del new['prices'][pid][s]
                dropped.append((s, pid, age))
            else:
                kept += 1
    for pid in [p for p, v in new['prices'].items() if not v]:
        del new['prices'][pid]
    updated_shops = [s for s in shops if shop_ok[s] and any(k[0] == s for k in picked)]
    if updated_shops:
        new['checked'], new['label'] = today, label
        nm = [cfg['shops'][s]['name'] for s in updated_shops]
        new['autoNote'] = (', '.join(nm[:-1]) + ' and ' + nm[-1] if len(nm) > 1 else nm[0]) + \
            ' prices are refreshed automatically (everyday items ' + {'day': 'every day', '2days': 'every 2 days'}.get(a.freq, 'every week') + \
            ', other items every few weeks).'
        new['run'] = {'date': today, 'shops': {s: {'searches': st[s]['ok'] + st[s]['err'], 'matched': sum(1 for k in picked if k[0] == s)} for s in shops}}

    # ---- report
    L = [f'# Plateful price update: {a.mode} run, {label}', '',
         f'Ingredients searched: {len(pids)}{due_note}. Searches made: {sum(v["ok"] + v["err"] for v in st.values())}. ' +
         (f'Estimated cost: about ${spent["usd"]:.2f} (worst case; Apify bills per result returned).' if a.source == 'apify'
          else 'Checked by the home price checker in a browser on your computer (no running cost).'), '']
    if fatal['msg']:
        L += [f'**Run stopped early:** {fatal["msg"]}', '']
    L += ['## Summary by shop', '', '| Shop | Searches ok | Failed | Returned nothing | Ingredients matched | Used this run? |', '|---|---|---|---|---|---|']
    for s in shops:
        used = 'yes' if shop_ok[s] else 'NO: ' + (st[s]['dead'] or 'unknown')
        L.append(f'| {cfg["shops"][s]["name"]} | {st[s]["ok"]} | {st[s]["err"]} | {st[s]["empty"]} | {sum(1 for k in picked if k[0] == s)} | {used} |')
    L.append('')
    if a.mode == 'test' and not a.fixtures:
        L += ['_Test run: prices.json was NOT changed. Raw samples of what each shop returned are in pipeline/samples/._', '']
    for s in shops:
        if st[s]['errors']:
            L += [f'### Errors from {cfg["shops"][s]["name"]}', ''] + [f'- {e}' for e in st[s]['errors'][:10]]
            if len(st[s]['errors']) > 10:
                L.append(f'- ... and {len(st[s]["errors"]) - 10} more')
            L.append('')
    L += ['## What it picked', '', 'One line per pack size: the cheapest matching product by price per gram (or per item).', '']
    for s in shops:
        rows = [(pid, picks) for (ss, pid), picks in picked.items() if ss == s]
        L += [f'### {cfg["shops"][s]["name"]} ({len(rows)} ingredients)', '']
        for pid, picks in rows:
            for slot in sorted(picks):
                p = picks[slot]
                sale = (f', ON SALE now {gbp(p["sale"])}' if p['sale'] is not None else '') + \
                       (f', {cfg["shops"][s].get("card", "loyalty card")} price {gbp(p["card"])}' if p.get('card') is not None else '')
                L.append(f'- **{cat[pid]["name"]}**: {p["name"]}, {p["size"]}{ {"g": "g", "ml": "ml"}.get(cat[pid]["unit"], " items") }, {gbp(p["price"])}{sale}'
                         f' (beat {p["others"]} other match{"es" if p["others"] != 1 else ""})')
        L.append('')
    L += ['## Searched but nothing usable found', '',
          'For each one, the nearest rejected products and why. If a good product is listed here, the rule for that ingredient needs loosening in pipeline/config.json.', '']
    for s, pid, n in unmatched:
        if not shop_ok[s]:
            continue
        L.append(f'- **{cfg["shops"][s]["name"]}, {cat[pid]["name"]}** ({n} results)')
        for _rank, nm, price, why in sorted(traces[(s, pid)], key=lambda t: -t[0])[:4]:
            L.append(f'    - {nm} {gbp(price) if price else ""}: {why}')
    L.append('')
    if dropped:
        L += ['## Old prices removed (not refreshed for over %d days)' % cfg['stale_days'], ''] + \
             [f'- {cfg["shops"].get(s, {}).get("name", s)}, {cat[pid]["name"]}: {age} days old' for s, pid, age in dropped] + ['']
    if skipped:
        L += ['## Skipped on purpose', ''] + [f'- {cat[p]["name"]}: {why}' for p, why in skipped] + ['']
    big = []
    for s, pid, prev, entry in changes:
        if prev:
            po = {p[0]: p[1] for p in prev['packs']}
            for p in entry['packs']:
                if p[0] in po and po[p[0]] and abs(p[1] - po[p[0]]) / po[p[0]] > 0.3:
                    big.append(f'- {cfg["shops"][s]["name"]}, {cat[pid]["name"]}, {p[0]}: {gbp(po[p[0]])} -> {gbp(p[1])}')
    if big:
        L += ['## Big price moves (over 30%) worth a look', ''] + big + ['']
    L += ['## Kept as they were', '', f'{kept} older prices were not refreshed this run (the shop returned no usable match) and were left in place.', '']
    with open(a.report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')

    # ---- write files
    if a.mode == 'test':
        if not a.fixtures:
            for (s, pid), raw in raws.items():
                d = os.path.join(a.samples_dir, s)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, pid + '.json'), 'w', encoding='utf-8') as f:
                    json.dump(raw, f, indent=1, ensure_ascii=False)
    elif not a.dry_run:
        if updated_shops:
            with open(a.prices, 'w', encoding='utf-8') as f:
                f.write(dump_prices(new, cat))
        else:
            print('No shop returned usable data, so prices.json was left unchanged.')
    print(f'Report written to {a.report}')
    if not any(shop_ok.values()):
        print('ERROR: every shop failed this run.')
        sys.exit(1)


if __name__ == '__main__':
    main()
