#!/usr/bin/env python3
"""
Fairbasket: daily prices for Tesco, Sainsbury's, Asda, Morrisons and Waitrose from the Pepesto grocery API.

What it does, in plain words
  1. Sends the names of the site's products to Pepesto as a shopping list, one list per shop (a list holds up to
     5,000 characters, so about 2 lists per shop). Pepesto answers with several real shop products per line, each
     with its price, pack size, offer and product link.
  2. Optionally (--searches missing) asks Pepesto's slower full-range search for products its daily list did not
     have (drinks, snacks, household ...). These cost more, so they are capped by --max-searches and --max-spend.
  3. Turns the answers into the same shape the other robots use, drops any product a person has blocked in
     pipeline/product-map.json, and hands everything to the SAME matching rules (pipeline/update_prices.py):
     right product, right pack size, sensible price. Products without hand-written rules use the starter rules
     made by pipeline/auto_rules.py.
  4. Last checks (pipeline/price_sources.py): big jumps or prices far from the other shops are held back; a fresh
     price typed in by hand is never overwritten; every price is marked "src": "pepesto"; Pepesto prices older
     than 3 days are removed (their terms allow keeping them for 3 days), so the robot must run daily.
  5. Records each match in pipeline/product-map.json and lists new or changed matches in the report.
  6. "live" mode saves prices.json; "test" mode only writes pipeline/pepesto-report.md.

Needs Python 3.9+ only. The key comes from the PEPESTO_API_KEY environment variable (a GitHub secret) and is never
printed or saved.

Usage
  python pipeline/pepesto_prices.py --mode test                     first 30 products, report only
  python pipeline/pepesto_prices.py --mode test --limit 0           every product, report only (the coverage test)
  python pipeline/pepesto_prices.py --mode live
  python pipeline/pepesto_prices.py --fixtures DIR ...              saved Pepesto answers instead of the API (tests)
"""
import argparse, collections, json, os, re, sys, tempfile, time
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import price_sources as PS  # noqa: E402
import auto_rules as AR     # noqa: E402

BASE = os.environ.get('PEPESTO_BASE') or 'https://s.pepesto.com/api'
DOMAINS = {
    'tesco': 'tesco.com',
    'sainsburys': 'sainsburys.co.uk',
    'asda': 'asda.com',
    'morrisons': 'groceries.morrisons.com',
    'waitrose': 'waitrose.com',
}
DEFAULT_SHOPS = ','.join(DOMAINS)
LIST_CHARS = 4800                 # Pepesto allows 5,000 characters per shopping list
EST_LIST_EUR = 0.32               # planning figure per list request (Starter price); the real charge is read back
EST_SEARCH_EUR = 0.12             # planning figure per full-range search (Starter price)
REPORT = os.path.join(HERE, 'pepesto-report.md')
MAP_FILE = os.path.join(HERE, 'product-map.json')


class Refused(Exception):
    pass


# ----------------------------------------------------------------------------- talking to Pepesto
class Api:
    def __init__(self, key, budget, fixtures=None, raw_dir=None):
        self.key, self.budget, self.fixtures, self.raw_dir = key, budget, fixtures, raw_dir
        self.spent = 0.0              # euros, from the Pepesto-Eurocents-Charged header
        self.calls = collections.Counter()

    def _post(self, path, body, est, tag):
        if self.fixtures:
            fp = os.path.join(self.fixtures, tag + '.json')
            self.calls[path] += 1
            if not os.path.exists(fp):
                return None
            data = json.load(open(fp, encoding='utf-8'))
            self.spent += float(data.pop('_eurocents', est * 100)) / 100 if isinstance(data, dict) else 0
            return data
        if self.spent + est > self.budget:
            raise Refused(f'spending limit €{self.budget:.2f} reached')
        req = urllib.request.Request(BASE.rstrip('/') + path, data=json.dumps(body).encode(), method='POST', headers={
            'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json',
            'User-Agent': 'FairbasketPriceBot/1.0 (+https://fairbasket.co.uk/; contact@fairbasket.co.uk)'})
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    charged = r.headers.get('Pepesto-Eurocents-Charged')
                    data = json.loads(r.read().decode('utf-8', 'replace'))
                self.calls[path] += 1
                self.spent += (float(charged) / 100) if charged not in (None, '') else est
                if self.raw_dir:
                    with open(os.path.join(self.raw_dir, tag + '.json'), 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False)
                return data
            except urllib.error.HTTPError as e:
                text = e.read().decode('utf-8', 'replace')[:200]
                if e.code in (401, 403):
                    sys.exit(f'Pepesto refused the key (HTTP {e.code}). Check the PEPESTO_API_KEY secret.')
                if e.code == 402:
                    sys.exit('Pepesto says the credit has run out (HTTP 402). Top up, or move to the Growth plan.')
                last = f'HTTP {e.code} {text}'
                if e.code < 500 and e.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                last = f'{type(e).__name__}: {e}'
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(last)

    def products(self, shop, lines, tag):
        return self._post('/products', {'manual_shopping_list': '\n'.join(lines), 'supermarket_domain': DOMAINS[shop],
                                        'item_names_locale': 'en-GB'}, EST_LIST_EUR, tag)

    def search(self, shop, phrase, tag, wait):
        """Full-range search: start it, then collect the answer (Pepesto says usually within 10 minutes)."""
        if self.fixtures:
            return self._post('/search', {}, EST_SEARCH_EUR, tag)
        started = self._post('/search', {'product': phrase, 'supermarket_domain': DOMAINS[shop]}, EST_SEARCH_EUR, tag + '-start')
        sid = (started or {}).get('search_session_id')
        if not sid:
            return None
        return sid

    def collect(self, sid, tag):
        data = self._post('/retrieve', {'search_session_id': sid}, 0.0, tag)
        return data


# ----------------------------------------------------------------------------- reading Pepesto's answer
def _key(text):
    t = re.sub(r'\(.*?\)', ' ', str(text).lower()).replace('&', ' and ')
    return ' '.join(re.findall(r'[a-z0-9%]+', t))


def map_items(answer, lines):
    """Pepesto echoes each shopping-list line as item_name (sometimes reworded). -> {line_index: [products]}"""
    items = (answer or {}).get('items') or []
    keys = [_key(x) for x in lines]
    out, used = {}, set()
    for n, it in enumerate(items):
        name = _key(it.get('item_name') or '')
        idx = keys.index(name) if name in keys and keys.index(name) not in used else None
        if idx is None and name:
            words = set(name.split())
            best = max(((len(words & set(k.split())) / max(1, len(words | set(k.split()))), i)
                        for i, k in enumerate(keys) if i not in used), default=(0, None))
            if best[0] >= 0.5:
                idx = best[1]
        if idx is None and len(items) == len(lines) and n not in used:
            idx = n                                  # same number of lines back as sent: keep the order
        if idx is None:
            continue
        used.add(idx)
        out[idx] = [p.get('product') or p for p in (it.get('products') or []) if isinstance(p, dict)]
    return out


def _pounds(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v) / 100, 2)               # Pepesto gives prices in pence
    if isinstance(v, dict):
        for k in ('price', 'value', 'amount'):
            if isinstance(v.get(k), (int, float)):
                return round(float(v[k]) / 100, 2)
    return None


def to_row(p, promo_mode='price-is-offer'):
    """One Pepesto product -> the row shape update_prices.normalise() reads. None if unusable."""
    if not isinstance(p, dict) or not p.get('product_name'):
        return None
    cls = p.get('classification') or {}
    if cls.get('is_substitution'):
        return None                                   # a stand-in for something else, not the product itself
    pr = p.get('price') if isinstance(p.get('price'), dict) else {'price': p.get('price')}
    price = _pounds(pr.get('price'))
    if not price:
        return None
    was = None
    for k in ('original_price', 'regular_price', 'standard_price', 'was_price', 'price_before_promo'):
        if pr.get(k) is not None:
            was = _pounds(pr[k])
            break
    for k in ('promo_price', 'promotional_price', 'sale_price'):
        if pr.get(k) is not None and _pounds(pr[k]) and _pounds(pr[k]) < price:
            was, price = price, _pounds(pr[k])
            break
    promo = pr.get('promotion') if isinstance(pr.get('promotion'), dict) else {}
    pct = promo.get('promo_percentage')
    text = ''
    if promo.get('promo'):
        text = f'{pct:g}% off' if isinstance(pct, (int, float)) and pct else 'On offer'
        if was is None and isinstance(pct, (int, float)) and 0 < pct < 90 and promo_mode == 'price-is-offer':
            was = round(price / (1 - pct / 100), 2)
        elif was is None and isinstance(pct, (int, float)) and 0 < pct < 90 and promo_mode == 'price-is-regular':
            was, price = price, round(price * (1 - pct / 100), 2)
    name = str(p['product_name']).strip()
    if cls.get('is_frozen') and 'frozen' not in name.lower():
        name += ' (frozen)'
    q = p.get('quantity') or {}
    size = ''
    if q.get('grams'):
        size = f'{float(q["grams"]):g}g'
    elif q.get('milliliters'):
        size = f'{float(q["milliliters"]):g}ml'
    elif q.get('pieces'):
        size = f'{int(q["pieces"])} pack'
    elif p.get('quantity_str'):
        size = str(p['quantity_str'])
    row = {'name': name, 'price': price, 'size': size}
    if was and was > price:
        row['originalPrice'] = was
    if text:
        row['promotion'] = text
    unit = p.get('price_per_meausure_unit') or p.get('price_per_measure_unit')
    if isinstance(unit, str) and '/' in unit:
        row['pricePerUnit'] = '£' + unit.replace(' ', '')
    url = p.get('product_id')
    if isinstance(url, str) and url.startswith('https://'):
        row['url'] = url
    if p.get('gtin14'):
        row['gtin'] = p['gtin14'] if isinstance(p['gtin14'], list) else [p['gtin14']]
    return row


def batches(lines, limit=LIST_CHARS):
    out, cur, n = [], [], 0
    for i, ln in enumerate(lines):
        if cur and n + len(ln) + 1 > limit:
            out.append(cur)
            cur, n = [], 0
        cur.append(i)
        n += len(ln) + 1
    if cur:
        out.append(cur)
    return out


def spread(ids, n):
    if n >= len(ids):
        return list(ids)
    step = len(ids) / n
    return [ids[int(i * step)] for i in range(n)]


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['test', 'live'], default='test', help='test = report only, prices.json is not changed')
    ap.add_argument('--limit', type=int, default=30, help='test only: how many products (0 = all)')
    ap.add_argument('--only', help='comma-separated product ids')
    ap.add_argument('--shops', default=DEFAULT_SHOPS)
    ap.add_argument('--rules', choices=['all', 'hand'], default='all',
                    help='all = hand-written rules plus the starter rules in auto-rules.json; hand = config.json only')
    ap.add_argument('--searches', choices=['off', 'missing'], default='off',
                    help='missing = full-range search for products the daily list did not have (dearer)')
    ap.add_argument('--max-searches', type=int, default=100)
    ap.add_argument('--search-wait', type=int, default=1500, help='seconds to wait for full-range searches')
    ap.add_argument('--max-spend', type=float, default=3.00, help='euros: stop calling Pepesto once this much is spent')
    ap.add_argument('--promo-mode', choices=['price-is-offer', 'price-is-regular'], default='price-is-offer',
                    help='how to read a Pepesto price that is marked as on promotion (checked in the first real test)')
    ap.add_argument('--fixtures', help='read saved Pepesto answers from DIR/<tag>.json instead of calling the API')
    ap.add_argument('--html', default=os.path.join(ROOT, 'index.html'))
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--auto-rules', default=os.path.join(HERE, 'auto-rules.json'))
    ap.add_argument('--prices', default=os.path.join(ROOT, 'prices.json'))
    ap.add_argument('--map', default=MAP_FILE)
    ap.add_argument('--report', default=REPORT)
    ap.add_argument('--raw-dir', default=os.path.join(HERE, 'pepesto-raw'))
    ap.add_argument('--today')
    a = ap.parse_args()

    U = PS.load_updater()
    hand = json.load(open(a.config, encoding='utf-8'))
    html = open(a.html, encoding='utf-8').read()
    cat_all = U.read_catalogue(html)
    if a.rules == 'hand':
        cfg = hand
    elif os.path.exists(a.auto_rules):
        cfg = AR.load_with_auto(hand, a.auto_rules)
    else:                                   # no saved file: make the starter rules now, from the site as it is today
        cfg = dict(hand, products=dict(hand['products']))
        for pid, r in AR.build(hand, cat_all, html).items():
            cfg['products'].setdefault(pid, r)
    tracked = [p for p in cat_all if p in cfg['products'] and not cfg['products'][p].get('skip')]
    today = a.today or PS.today_utc()
    shops = [s for s in a.shops.split(',') if s]
    for s in shops:
        if s not in DOMAINS:
            sys.exit(f'Unknown shop "{s}". Pepesto shops: {", ".join(DOMAINS)}')
    names = PS.shop_names(cfg)

    if a.only:
        ids = [p for p in a.only.split(',') if p]
        bad = [p for p in ids if p not in tracked]
        if bad:
            sys.exit('No rules for: ' + ', '.join(bad))
    elif a.mode == 'test' and a.limit:
        first = [p for p in cfg.get('test_products', []) if p in tracked]
        ids = (first + spread([p for p in tracked if p not in first], max(0, a.limit - len(first))))[:a.limit]
    else:
        ids = list(tracked)
    lines = [cfg['products'][p]['kw'] for p in ids]
    plan = batches(lines)
    n_auto = sum(1 for p in ids if cfg['products'][p].get('auto'))
    print(f'{a.mode} mode: {len(ids)} products ({n_auto} on starter rules) at {", ".join(names[s] for s in shops)}: '
          f'{len(plan)} list request(s) per shop, {len(plan) * len(shops)} in all. Spending limit €{a.max_spend:.2f}.')

    key = os.environ.get('PEPESTO_API_KEY', '')
    if not a.fixtures and not key:
        sys.exit('PEPESTO_API_KEY is not set. In GitHub: Settings > Secrets and variables > Actions > New repository secret.')
    if not a.fixtures:
        os.makedirs(a.raw_dir, exist_ok=True)
    api = Api(key, a.max_spend, a.fixtures, None if a.fixtures else a.raw_dir)
    pmap = PS.ProductMap(a.map)
    old = json.load(open(a.prices, encoding='utf-8')) if os.path.exists(a.prices) else {'prices': {}}

    per = {(s, p): [] for s in shops for p in ids}
    gtins = {}
    stats = collections.Counter()
    missing = collections.defaultdict(list)      # shop -> product ids Pepesto's daily list did not have
    errors = []

    def take(s, pid, products):
        blocked = pmap.blocked(pid, s)
        whole = cfg['products'][pid].get('whole_pack')
        for p in products:
            row = to_row(p, a.promo_mode)
            if not row:
                stats['unusable'] += 1
                continue
            if whole:
                row['size'] = '1 pack'           # the site counts the whole pack/bottle/loaf as one
            if row.get('url') in blocked:
                stats['blocked by product map'] += 1
                continue
            if row.get('gtin') and row.get('url'):
                gtins[row['url']] = row['gtin']
            row.pop('gtin', None)
            if row not in per[(s, pid)]:
                per[(s, pid)].append(row)
                stats['products returned'] += 1

    stopped = False
    for s in shops:
        for b, idx in enumerate(plan):
            tag = f'products-{s}-{b + 1}'
            try:
                ans = api.products(s, [lines[i] for i in idx], tag)
            except Refused as e:
                errors.append(f'{names[s]}: {e}; {len(plan) - b} list(s) not sent')
                stopped = True
                break
            except RuntimeError as e:
                errors.append(f'{names[s]} list {b + 1}: {e}')
                continue
            if ans is None:
                continue
            got = map_items(ans, [lines[i] for i in idx])
            for j, i in enumerate(idx):
                pid = ids[i]
                if got.get(j):
                    take(s, pid, got[j])
                else:
                    missing[s].append(pid)
            print(f'  {names[s]} list {b + 1}/{len(plan)}: {len(got)} of {len(idx)} lines answered (spent €{api.spent:.2f})')
        if stopped:
            break

    searched = collections.Counter()
    if a.searches == 'missing' and not stopped:
        want = [(s, pid) for s in shops for pid in missing[s] if s not in cat_all[pid]['miss']][:a.max_searches]
        print(f'Full-range searches for products the daily list did not have: {len(want)}')
        pending = {}
        for s, pid in want:
            tag = f'search-{s}-{pid}'
            try:
                res = api.search(s, cfg['products'][pid]['kw'], tag, a.search_wait)
            except Refused as e:
                errors.append(f'searches stopped: {e}')
                break
            except RuntimeError as e:
                errors.append(f'{names[s]} search {pid}: {e}')
                continue
            if a.fixtures:
                if res:
                    got = map_items(res, [cfg['products'][pid]['kw']])
                    take(s, pid, got.get(0, []))
                    searched[s] += 1
            elif res:
                pending[res] = (s, pid, tag)
        started = time.time()
        while pending and time.time() - started < a.search_wait:
            time.sleep(float(os.environ.get('PEPESTO_POLL_SECONDS') or 60))
            for sid, (s, pid, tag) in list(pending.items()):
                try:
                    r = api.collect(sid, tag)
                except (RuntimeError, Refused):
                    continue
                if (r or {}).get('status') == 'done' or (r or {}).get('items'):
                    got = map_items(r, [cfg['products'][pid]['kw']])
                    take(s, pid, got.get(0, []))
                    searched[s] += 1
                    pending.pop(sid)
        if pending:
            errors.append(f'{len(pending)} full-range searches did not finish in {a.search_wait}s')

    answered = sorted({p for (s, p), rows in per.items() if rows})
    if not answered:
        sys.exit('Pepesto returned nothing usable, so nothing was changed.' + (' ' + '; '.join(errors) if errors else ''))

    work = tempfile.mkdtemp(prefix='pepesto-')
    fx = os.path.join(work, 'fixtures')
    for (s, pid), rows in per.items():
        os.makedirs(os.path.join(fx, s), exist_ok=True)
        with open(os.path.join(fx, s, pid + '.json'), 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False)
    out_prices = os.path.join(work, 'prices.json')
    code, log = PS.run_matcher(PS.matcher_config(cfg, shops, 'pepesto'), work, fx, shops, answered, a.html,
                               a.prices, out_prices, a.report, today, 'live')
    print('\n'.join(log.strip().splitlines()[-6:]))
    if code != 0:
        sys.exit('The matching step failed (see above). prices.json was not changed.')
    new = json.load(open(out_prices, encoding='utf-8'))
    held = PS.guard(old, new, shops, today, names)
    PS.keep_recent_packs(old, new, shops, today)
    PS.tag_source(new, old, shops, today, 'pepesto')
    kept = PS.respect_priority(old, new, shops, today, names)
    expired = PS.expire(new, today)
    pmap.record(new, old, shops, today, gtins, names)

    # coverage table: products with a fresh Pepesto price, per shop
    cov = []
    for s in shops:
        live = sum(1 for p in ids if new['prices'].get(p, {}).get(s, {}).get('checked') == today)
        stocked = sum(1 for p in ids if s not in cat_all[p]['miss'])
        cov.append(f'| {names[s]} | {stocked} | {len(ids) - len(missing[s])} | {live} | {round(100 * live / max(1, stocked))}% |'
                   + (f' {searched[s]} searched |' if a.searches == 'missing' else ''))
    if a.mode == 'live':
        PS.finish(U, cat_all, a.prices, new)
        pmap.save()

    rep = open(a.report, encoding='utf-8').read().split('\n')
    head = [f'# Fairbasket Pepesto update: {a.mode} run, {today}', '',
            '_Test run: prices.json was NOT changed. The lines below show what a live run would do._' if a.mode == 'test' else '', '',
            f'Products sent: {len(ids)} ({n_auto} on starter rules). Pepesto calls: '
            + ', '.join(f'{k} x{v}' for k, v in api.calls.items()) + f'. Spent about €{api.spent:.2f}. '
            f'Products returned: {stats["products returned"]}; unusable: {stats["unusable"]}; blocked by the product map: '
            f'{stats["blocked by product map"]}. Old Pepesto prices removed (over 3 days): {expired}.', '',
            '## Coverage', '',
            '| Shop | Products it stocks | Pepesto had | Live price after matching | Share |' + (' Full searches |' if a.searches == 'missing' else ''),
            '| --- | --- | --- | --- | --- |' + (' --- |' if a.searches == 'missing' else '')] + cov + ['']
    if errors:
        head += ['## Problems', ''] + [f'- {e}' for e in errors] + ['']
    body = [ln for ln in rep[1:] if not ln.startswith('Ingredients searched:')]
    with open(a.report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(head + held + kept + pmap.report() + body))
    if a.mode == 'test':
        print('Test run: prices.json was NOT changed.')
    print(f'Report written to {a.report}. Spent about €{api.spent:.2f}. Held back: {sum(1 for x in held if x.startswith("- "))}.')


if __name__ == '__main__':
    main()
