#!/usr/bin/env python3
"""
Fairbasket: prices from UK Google Shopping (through DataForSEO) for Tesco, Sainsbury's, Asda, Waitrose and Iceland.
(Morrisons is checked on its own website by the home price checker; Aldi by pipeline/aldi_prices.py.)

What it does, in plain words
  1. Picks the ingredients to check (the ones that have rules in pipeline/config.json).
  2. Runs one UK Google Shopping search per ingredient (about a tenth of a US cent each).
  3. Optionally (--shop-searches missing) searches again as "<shop> <ingredient>" for each shop that had nothing
     usable, which finds more of the shops' own-label products.
  4. Keeps only listings sold by the shops above, and only the shops' OWN-LABEL products (Tesco, Stockwell & Co,
     Sainsbury's, Stamford Street, Asda, Just Essentials, Waitrose, essential Waitrose, Iceland ...). A brand is only
     used where the price on the site for that shop is already a brand. This stops a premium brand (Clarence Court
     eggs, Cathedral City cheddar) from standing in for the shop's normal price.
  5. Google often leaves the pack size out of the name ("Tesco Free Range Eggs"). The size is filled in only when the
     same shop's same product name was seen WITH a size before (in an earlier Google listing or in the prices
     already on the site) and the price is within 35% of that time. Learned names are kept in google-sizes.json.
  6. Hands the listings to the SAME matching rules the other robots use (pipeline/update_prices.py).
  7. Last check (price_sources.guard): a price that jumps by more than half, falls by more than 40%, or is far from
     the other shops is held back and listed in the report; the old price stays.
  8. "live" mode saves prices.json; "test" mode only writes the report.

Needs only Python 3.9+. Login and password come from DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD (GitHub secrets).

Usage
  python pipeline/google_shopping_prices.py --mode test                        30 ingredients, report only
  python pipeline/google_shopping_prices.py --mode test --shop-searches missing
  python pipeline/google_shopping_prices.py --mode live --scope all
  python pipeline/google_shopping_prices.py --fixtures DIR ...                 saved DataForSEO answers (for tests)
"""
import argparse, base64, collections, datetime as dt, json, os, re, sys, tempfile, time
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import price_sources as PS  # noqa: E402

# who sold it: shop id -> words in the seller name or web address
SELLERS = {
    'tesco': r'tesco',
    'sainsburys': r'sainsbury',
    'asda': r'\basda\b|asda\.com',
    'morrisons': r'morrisons',
    'waitrose': r'waitrose',
    'iceland': r'\biceland\b|iceland\.co\.uk',
}
DEFAULT_SHOPS = 'tesco,sainsburys,asda,waitrose,iceland'
# the shops' own labels (normal and value ranges; premium ranges are rejected by the "premium" words in config.json)
OWN_LABEL = {
    'tesco': r"\btesco\b|stockwell|grower'?s harvest|hearty food|creamfields|ms\.? molly|eastman'?s|boswell farms|"
             r"woodside farms|willow farms|nightingale farms|redmere farms|rosedene farms|suntrail farms|oakpark farms|"
             r"butcher'?s choice|nevill'?s|bay fishmongers|\bromano\b|\bwessex\b",
    'sainsburys': r"sainsbury|stamford street|\bso organic\b",
    'asda': r"\basda\b|just essentials|smart price|farm stores",
    'morrisons': r"morrisons|\bsavers\b|market street",
    'waitrose': r"waitrose|\bessential\b",
    'iceland': r"\biceland\b",
}
EXTRA_AVOID = re.compile(r"\bno\.? ?1\b|\bfinest\b|taste the difference|extra special|\bthe best\b|\bduchy\b|"
                         r"cater[- ]?now|catering|\bbulk\b|case of", re.I)
COST_PER_SEARCH = 0.0012     # US dollars; measured about 0.001, this is the safety allowance
BASE = os.environ.get('DATAFORSEO_BASE') or 'https://api.dataforseo.com'
SIZES_FILE = os.path.join(HERE, 'google-sizes.json')


# ----------------------------------------------------------------------------- DataForSEO
def call(path, login, password, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE.rstrip('/') + path, data=data, method='POST' if body is not None else 'GET', headers={
        'Authorization': 'Basic ' + base64.b64encode(f'{login}:{password}'.encode()).decode(),
        'Content-Type': 'application/json', 'User-Agent': 'fairbasket-google-shopping-prices'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        text = e.read().decode('utf-8', 'replace')[:300]
        if e.code in (401, 403):
            sys.exit('DataForSEO refused the login (HTTP %d). Check the DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD secrets '
                     '(the API password from the API Access page, not your website password).' % e.code)
        raise RuntimeError(f'HTTP {e.code} {text}')


def search(queries, login, password, wait, budget, spent):
    """queries: {tag: keyword}. Returns ({tag: result}, spent)."""
    tags = list(queries)
    pending, results = {}, {}
    for i in range(0, len(tags), 100):
        chunk = [{'keyword': queries[t], 'location_code': 2826, 'language_code': 'en', 'depth': 40, 'tag': t} for t in tags[i:i + 100]]
        if spent + len(chunk) * COST_PER_SEARCH > budget:
            print(f'  spending limit ${budget:.2f} reached, {len(tags) - i} searches not made')
            break
        posted = call('/v3/merchant/google/products/task_post', login, password, chunk)
        spent += float(posted.get('cost') or 0)
        for t in posted.get('tasks', []):
            code = t.get('status_code')
            if code == 20100 and t.get('id'):
                pending[t['id']] = (t.get('data') or {}).get('tag') or ''
            else:
                print(f'  one search was not accepted: {code} {t.get("status_message")}')
                if code in (40200, 40201):
                    sys.exit('DataForSEO says the account is out of credit (%s %s). Add credit on the DataForSEO dashboard.'
                             % (code, t.get('status_message')))
    print(f'  {len(pending)} searches accepted, waiting for Google ...')
    started = time.time()
    while pending and time.time() - started < wait:
        time.sleep(float(os.environ.get('GSHOP_POLL_SECONDS') or 15))
        for tid in list(pending):
            try:
                r = call(f'/v3/merchant/google/products/task_get/advanced/{tid}', login, password)
            except RuntimeError as e:
                print('  retrying later:', str(e)[:100])
                continue
            for t in r.get('tasks', []):
                code = t.get('status_code')
                if code == 20000:
                    spent += float(t.get('cost') or 0)
                    results[pending.pop(tid)] = t.get('result')
                elif code not in (40601, 40602):
                    print(f'  {pending[tid]}: failed ({code} {t.get("status_message")})')
                    pending.pop(tid)
        print(f'  {len(results)} done, {len(pending)} waiting ({int(time.time() - started)}s)')
    if pending:
        print(f'  gave up on {len(pending)} searches that did not finish in {wait}s')
    return results, spent


# ----------------------------------------------------------------------------- reading Google's answer
def walk(node, out):
    if isinstance(node, dict):
        if node.get('title') and (isinstance(node.get('price'), (int, float)) or node.get('seller') or node.get('domain')):
            out.append(node)
        for v in node.values():
            walk(v, out)
    elif isinstance(node, list):
        for v in node:
            walk(v, out)


def which_shop(listing):
    hay = ' '.join(str(listing.get(k) or '') for k in ('seller', 'domain', 'shop_name')).lower()
    for sid, rx in SELLERS.items():
        if re.search(rx, hay):
            return sid
    return None


def num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ('current', 'value', 'amount'):
            if isinstance(v.get(k), (int, float)):
                return float(v[k])
    return None


def listings(result):
    """[(shop, title, price, old_price)] from one Google answer, without repeats."""
    found, out, seen = [], [], set()
    walk(result, found)
    for it in found:
        sid, price = which_shop(it), num(it.get('price'))
        title = str(it.get('title') or '').strip()
        if not sid or not price or not title:
            continue
        key = (sid, title.lower(), price)
        if key in seen:
            continue
        seen.add(key)
        out.append((sid, title, price, num(it.get('old_price'))))
    return out


# ----------------------------------------------------------------------------- pack sizes learned from names
class SizeMemory:
    """Remembers, per shop, product names that came with a pack size, so the same name without a size can be read."""

    def __init__(self, U, path):
        self.U, self.path = U, path
        self.data = {}
        if os.path.exists(path):
            try:
                self.data = json.load(open(path, encoding='utf-8'))
            except ValueError:
                self.data = {}
        self.added = 0

    def key(self, shop, name):
        t = str(name).lower().replace('&', ' and ').replace('’', "'")
        t = re.sub(r"'", '', t)
        t = self.U.MULT_RE.sub(' ', t)
        t = self.U.QTY_RE.sub(' ', t)
        for rx in self.U.COUNT_RES[:5]:
            t = rx.sub(' ', t)
        t = re.sub(r'\b(?:pack|pk|each|approx|approximately|typically)\b', ' ', t)
        t = re.sub(r'[^a-z0-9%]+', ' ', t).strip()
        return shop + '|' + t

    @staticmethod
    def size_text(q):
        if 'g' in q:
            return f'{q["g"]:g}g'
        if 'ml' in q:
            return f'{q["ml"]:g}ml'
        if 'count' in q:
            return f'{q["count"]} pack'
        return ''

    def learn(self, shop, name, price, today):
        q = self.U.parse_sizes(name)
        txt = self.size_text(q)
        if not txt or not price:
            return
        k = self.key(shop, name)
        if len(k.split('|', 1)[1]) < 6:
            return
        rows = self.data.setdefault(k, [])
        for r in rows:
            if r['size'] == txt:
                r['price'], r['seen'] = round(price, 2), today
                return
        rows.append({'size': txt, 'price': round(price, 2), 'seen': today})
        self.added += 1

    def size_for(self, shop, name, price):
        rows = self.data.get(self.key(shop, name)) or []
        sizes = {r['size'] for r in rows}
        if len(sizes) != 1:
            return None            # never seen, or seen in more than one size: do not guess
        r = rows[0]
        return r['size'] if r['price'] and 0.65 <= price / r['price'] <= 1.35 else None

    def save(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(dict(sorted(self.data.items())), f, ensure_ascii=False, indent=0)


# ----------------------------------------------------------------------------- main
def spread(ids, n):
    if n >= len(ids):
        return list(ids)
    step = len(ids) / n
    return [ids[int(i * step)] for i in range(n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['test', 'live'], default='test', help='test = report only, prices.json is not changed')
    ap.add_argument('--scope', choices=['all', 'due'], default='all')
    ap.add_argument('--limit', type=int, default=30, help='test only: how many ingredients to search')
    ap.add_argument('--only', help='comma-separated ingredient ids')
    ap.add_argument('--shops', default=DEFAULT_SHOPS)
    ap.add_argument('--shop-searches', choices=['off', 'missing'], default='off',
                    help='missing = a second "<shop> <ingredient>" search for each shop that had no usable listing')
    ap.add_argument('--fixtures', help='read saved DataForSEO answers from DIR/<tag>.json instead of calling the API')
    ap.add_argument('--html', default=os.path.join(ROOT, 'index.html'))
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--prices', default=os.path.join(ROOT, 'prices.json'))
    ap.add_argument('--report', default=os.path.join(HERE, 'google-shopping-report.md'))
    ap.add_argument('--raw-dir', default=os.path.join(HERE, 'google-shopping-raw'))
    ap.add_argument('--sizes', default=SIZES_FILE)
    ap.add_argument('--today')
    ap.add_argument('--wait', type=int, default=1500)
    ap.add_argument('--max-spend', type=float, default=1.50, help='stop making searches once this many US dollars are spent')
    a = ap.parse_args()

    U = PS.load_updater()
    cfg = json.load(open(a.config, encoding='utf-8'))
    cat_all = U.read_catalogue(open(a.html, encoding='utf-8').read())
    tracked = [p for p in cat_all if p in cfg['products'] and not cfg['products'][p].get('skip')]
    cat = {p: cat_all[p] for p in tracked}
    today = a.today or PS.today_utc()
    today_d = dt.date.fromisoformat(today)
    shops = [s for s in a.shops.split(',') if s]
    for s in shops:
        if s not in SELLERS:
            sys.exit(f'Unknown shop "{s}". Google Shopping shops: {", ".join(SELLERS)}')
    names = PS.shop_names(cfg)

    if a.only:
        ids = [p for p in a.only.split(',') if p]
        bad = [p for p in ids if p not in tracked]
        if bad:
            sys.exit('Not tracked (no rules in config.json, or skipped): ' + ', '.join(bad))
    elif a.mode == 'test':
        first = [p for p in cfg.get('test_products', []) if p in tracked]
        ids = (first + spread([p for p in tracked if p not in first], max(0, a.limit - len(first))))[:a.limit]
    elif a.scope == 'due':
        ids = [p for p in tracked if U.is_due(p, cfg['products'][p], today_d)]
    else:
        ids = list(tracked)
    est = len(ids) * COST_PER_SEARCH * (1 + (len(shops) if a.shop_searches == 'missing' else 0))
    print(f'{a.mode} mode: {len(ids)} ingredients at {", ".join(names[s] for s in shops)}. '
          f'Worst-case cost about ${est:.2f} (stops at ${a.max_spend:.2f}).')

    old = json.load(open(a.prices, encoding='utf-8')) if os.path.exists(a.prices) else {'prices': {}}
    memory = SizeMemory(U, a.sizes)
    for pid, by_shop in old.get('prices', {}).items():          # names with sizes already on the site
        for s, e in by_shop.items():
            if s in SELLERS:
                for pk, it in zip(e.get('packs', []), e.get('items', [])):
                    memory.learn(s, it.get('n', ''), pk[1] if len(pk) > 1 else None, e.get('checked') or today)
    own = {s: re.compile(OWN_LABEL[s], re.I) for s in SELLERS}
    brand_ok = {(pid, s) for pid, by_shop in old.get('prices', {}).items() for s, e in by_shop.items()
                if s in own and e.get('product') and not own[s].search(e['product'])}
    brand_ok |= {(pid, s) for pid in ids for s in shops if cfg['products'][pid].get('brand_ok')}

    login, password = os.environ.get('DATAFORSEO_LOGIN', ''), os.environ.get('DATAFORSEO_PASSWORD', '')
    if not a.fixtures and (not login or not password):
        sys.exit('DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD are not set. In GitHub: Settings > Secrets and variables > Actions.')
    os.makedirs(a.raw_dir, exist_ok=True)
    spent = 0.0

    def fetch(queries):
        nonlocal spent
        if a.fixtures:
            got = {}
            for t in queries:
                fp = os.path.join(a.fixtures, t + '.json')
                if os.path.exists(fp):
                    got[t] = json.load(open(fp, encoding='utf-8'))
            return got
        got, spent = search(queries, login, password, a.wait, a.max_spend, spent)
        for t, res in got.items():
            with open(os.path.join(a.raw_dir, t + '.json'), 'w', encoding='utf-8') as f:
                json.dump(res, f, ensure_ascii=False)
        return got

    stats = collections.Counter()
    per = {(s, p): [] for s in shops for p in ids}           # listings kept, per shop and ingredient
    answered = set()

    def take(pid, res, only_shop=None):
        answered.add(pid)
        for sid, title, price, was in listings(res):
            if sid not in shops or (only_shop and sid != only_shop):
                continue
            stats['listings'] += 1
            memory.learn(sid, title, price, today)
            if EXTRA_AVOID.search(title):
                stats['premium or bulk'] += 1
                continue
            if not own[sid].search(title) and (pid, sid) not in brand_ok:
                stats['brands (not own-label)'] += 1
                continue
            row = {'name': title, 'price': price}
            if was and was > price:
                row['originalPrice'] = was
            if not U.parse_sizes(title):
                size = memory.size_for(sid, title, price)
                if size:
                    row['size'] = size
                    stats['sizes filled from earlier names'] += 1
                else:
                    stats['no pack size'] += 1
            if row not in per[(sid, pid)]:
                per[(sid, pid)].append(row)

    print('Searching Google Shopping ...')
    for pid, res in fetch({p: cfg['products'][p]['kw'] for p in ids}).items():
        take(pid, res)

    if a.shop_searches == 'missing':
        matcher = U.Matcher(cfg, cat)
        need = {}
        for s in shops:
            for pid in ids:
                if s in cat[pid]['miss'] or pid not in answered:
                    continue
                picks, _ = matcher.choose(pid, [U.normalise(r) for r in per[(s, pid)]])
                if not picks:
                    need[f'{pid}__{s}'] = f'{names[s]} {cfg["products"][pid]["kw"]}'
        print(f'Second searches for shops with nothing usable: {len(need)}')
        for tag, res in fetch(need).items():
            pid, s = tag.rsplit('__', 1)
            take(pid, res, only_shop=s)

    if not answered:
        sys.exit('No results came back, so nothing was changed.')
    work = tempfile.mkdtemp(prefix='gshop-')
    fx = os.path.join(work, 'fixtures')
    for (s, pid), rows in per.items():
        os.makedirs(os.path.join(fx, s), exist_ok=True)
        with open(os.path.join(fx, s, pid + '.json'), 'w', encoding='utf-8') as f:
            json.dump(rows, f, ensure_ascii=False)

    searched = [p for p in ids if p in answered]
    out_prices = os.path.join(work, 'prices.json')
    # always worked out as a live run into a scratch copy; only a live run copies the result over prices.json
    code, log = PS.run_matcher(PS.matcher_config(cfg, shops, 'google-shopping'), work, fx, shops, searched, a.html,
                               a.prices, out_prices, a.report, today, 'live')
    print('\n'.join(log.strip().splitlines()[-6:]))
    if code != 0:
        sys.exit('The matching step failed (see above). prices.json was not changed.')
    new = json.load(open(out_prices, encoding='utf-8'))
    held = PS.guard(old, new, shops, today, names)
    PS.keep_recent_packs(old, new, shops, today)
    if a.mode == 'live':
        PS.finish(U, cat_all, a.prices, new)
        memory.save()

    rep = open(a.report, encoding='utf-8').read().split('\n')
    head = [f'# Fairbasket Google Shopping update: {a.mode} run, {today}', '',
            '_Test run: prices.json was NOT changed. The lines below show what a live run would do._' if a.mode == 'test' else '', '',
            f'Ingredients searched: {len(ids)}; answered: {len(answered)}. Cost about ${spent:.3f}. '
            f'Listings from the shops: {stats["listings"]}; left out as brands: {stats["brands (not own-label)"]}; '
            f'left out as premium/bulk: {stats["premium or bulk"]}; pack size filled from earlier names: '
            f'{stats["sizes filled from earlier names"]}; still no pack size: {stats["no pack size"]}.', '']
    body = [ln for ln in rep[1:] if not ln.startswith('Ingredients searched:')]
    with open(a.report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(head + held + body))
    if a.mode == 'test':
        print('Test run: prices.json was NOT changed.')
    print(f'Report written to {a.report}. Spent about ${spent:.3f}. Held back: {sum(1 for x in held if x.startswith("- "))}.')


if __name__ == '__main__':
    main()
