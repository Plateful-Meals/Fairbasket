#!/usr/bin/env python3
"""
Fairbasket: prices from UK Google Shopping (through DataForSEO), merged into prices.json.

What it does, in plain words
  1. Picks the ingredients to check (the ones that have rules in pipeline/config.json).
  2. Runs one UK Google Shopping search per ingredient (a paid API, about a tenth of a US cent per search).
  3. Keeps only the listings sold by Tesco, Sainsbury's, Asda, Morrisons, Waitrose and Iceland.
  4. Hands those listings to the SAME matching rules the Apify robot uses (pipeline/update_prices.py), so the same
     checks apply: right product, right pack size, no frozen/ready-meal mix-ups, price sanity checks.
  5. In "live" mode it merges the matches into prices.json. In "test" mode it only writes a report and changes nothing.

Anything it cannot match with confidence keeps the price it already had. A shop whose searches mostly find nothing
is ignored for that run (shop_min_hit_ratio in config.json).

Needs only Python 3.9+ (standard library). The login and password come from the environment variables
DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD (GitHub secrets) and are never printed or saved.

Usage
  python pipeline/google_shopping_prices.py --mode test                 30 ingredients, report only
  python pipeline/google_shopping_prices.py --mode test --limit 60
  python pipeline/google_shopping_prices.py --mode live --scope all     every tracked ingredient, updates prices.json
  python pipeline/google_shopping_prices.py --mode live --scope due     only the ingredients whose turn it is
  python pipeline/google_shopping_prices.py --fixtures DIR ...          read saved DataForSEO results instead of calling it
"""
import argparse, base64, collections, copy, datetime as dt, importlib.util, json, os, re, subprocess, sys, tempfile, time
import urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# shop id -> (words that identify it in a seller name or web address)
SELLERS = {
    'tesco': r'tesco',
    'sainsburys': r'sainsbury',
    'asda': r'\basda\b|asda\.com',
    'morrisons': r'morrisons',
    'waitrose': r'waitrose',
    'iceland': r'\biceland\b|iceland\.co\.uk',
}
# used only if a shop is missing from config.json
SHOP_DEFAULTS = {
    'tesco': ('Tesco', 'Clubcard'), 'sainsburys': ("Sainsbury's", 'Nectar'), 'asda': ('Asda', 'Asda Rewards'),
    'morrisons': ('Morrisons', 'More Card'), 'waitrose': ('Waitrose', 'myWaitrose'), 'iceland': ('Iceland', 'Bonus Card'),
}
COST_PER_SEARCH = 0.0015        # US dollars; the real figure is about 0.001, this is the safety allowance
BASE = os.environ.get('DATAFORSEO_BASE') or 'https://api.dataforseo.com'


def load_updater():
    spec = importlib.util.spec_from_file_location('update_prices', os.path.join(HERE, 'update_prices.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def call(base, path, login, password, body=None, timeout=60):
    url = base.rstrip('/') + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method='POST' if body is not None else 'GET', headers={
        'Authorization': 'Basic ' + base64.b64encode(f'{login}:{password}'.encode()).decode(),
        'Content-Type': 'application/json', 'User-Agent': 'fairbasket-google-shopping-prices'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        text = e.read().decode('utf-8', 'replace')[:300]
        if e.code in (401, 403):
            sys.exit('DataForSEO refused the login (HTTP %d). Check the DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD secrets. '
                     'The password is the API password from the API Access page, not your website password.' % e.code)
        raise RuntimeError(f'HTTP {e.code} {text}')


def walk(node, out):
    """Collect every dict that looks like a product listing: it has a title and either a price or a seller/domain."""
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


def extra_text(it):
    """Any extra words Google gives about a listing (snippet, description, tags): sometimes the pack size is only there."""
    parts = [str(it.get(k) or '') for k in ('snippet', 'description')]
    tags = it.get('tags')
    if isinstance(tags, list):
        parts += [str(t) for t in tags]
    return ' | '.join(x.strip() for x in parts if x and x.strip())[:200]


def to_fixtures(results, outdir, shops, U=None, examples=None):
    """results: {pid: DataForSEO task}.  Writes outdir/<shop>/<pid>.json in the shape update_prices.py reads."""
    seen_per_shop = collections.Counter()
    for pid, t in results.items():
        found = []
        walk(t.get('result'), found)
        per = {s: [] for s in shops}
        seen = set()
        for it in found:
            sid = which_shop(it)
            if sid not in per:
                continue
            price = num(it.get('price'))
            key = (str(it.get('title')), sid, price)
            if key in seen:
                continue
            seen.add(key)
            row = {'name': str(it.get('title')).strip(), 'price': price}
            extra = extra_text(it)
            if U is not None and extra and not U.parse_sizes(row['name']) and U.parse_sizes(extra):
                row['size'] = extra          # the title has no pack size but the extra words do
            if examples is not None and len(examples) < 14 and not U.parse_sizes(row['name']):
                examples.append(f'- {sid}, "{row["name"]}", £{price}: extra words = "{extra or "none"}"')
            was = num(it.get('old_price'))
            if was:
                row['originalPrice'] = was
            per[sid].append(row)
        for sid, rows in per.items():
            d = os.path.join(outdir, sid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, pid + '.json'), 'w', encoding='utf-8') as f:
                json.dump(rows, f, ensure_ascii=False, indent=1)
            seen_per_shop[sid] += bool(rows)
    return seen_per_shop


def search(ids, queries, login, password, wait, max_spend, outdir):
    tasks = [{'keyword': queries[p], 'location_code': 2826, 'language_code': 'en', 'depth': 40, 'tag': p} for p in ids]
    spent, pending, results = 0.0, {}, {}
    print(f'Posting {len(tasks)} Google Shopping searches ...')
    for i in range(0, len(tasks), 100):
        chunk = tasks[i:i + 100]
        posted = call(BASE, '/v3/merchant/google/products/task_post', login, password, chunk)
        spent += float(posted.get('cost') or 0)
        for t in posted.get('tasks', []):
            code = t.get('status_code')
            if code == 20100 and t.get('id'):
                pending[t['id']] = (t.get('data') or {}).get('tag') or ''
            else:
                print(f'  one search was not accepted: {code} {t.get("status_message")}')
                if code in (40200, 40201):
                    sys.exit('DataForSEO says the account is out of credit or not verified (%s %s). Add credit on the DataForSEO dashboard.'
                             % (code, t.get('status_message')))
        if spent > max_spend:
            sys.exit(f'Stopped: spending reached ${spent:.4f}, over the limit ${max_spend:.2f}.')
    if not pending:
        sys.exit('No search was accepted, so there is nothing to wait for.')
    print(f'{len(pending)} searches accepted (about ${spent:.4f} so far). Waiting for Google results ...')
    started = time.time()
    while pending and time.time() - started < wait:
        time.sleep(float(os.environ.get('GSHOP_POLL_SECONDS') or 20))
        for tid in list(pending):
            try:
                r = call(BASE, f'/v3/merchant/google/products/task_get/advanced/{tid}', login, password)
            except RuntimeError as e:
                print('  retrying later:', str(e)[:100])
                continue
            for t in r.get('tasks', []):
                code = t.get('status_code')
                if code == 20000:
                    spent += float(t.get('cost') or 0)
                    results[pending.pop(tid)] = t
                elif code in (40601, 40602):
                    pass
                else:
                    print(f'  {pending[tid]}: failed ({code} {t.get("status_message")})')
                    pending.pop(tid)
        print(f'  {len(results)} done, {len(pending)} waiting ({int(time.time() - started)}s)')
    if pending:
        print(f'Gave up on {len(pending)} searches that did not finish in {wait}s: {", ".join(pending.values())}')
    os.makedirs(outdir, exist_ok=True)
    for pid, t in results.items():     # keep the raw answers so any odd match can be inspected later
        with open(os.path.join(outdir, pid + '.json'), 'w', encoding='utf-8') as f:
            json.dump(t.get('result'), f, ensure_ascii=False)
    return results, spent


def spread(ids, n):
    """n ids spread evenly across the list (so a test covers meat, veg, tins, dairy... not just the first shelf)."""
    if n >= len(ids):
        return list(ids)
    step = len(ids) / n
    return [ids[int(i * step)] for i in range(n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['test', 'live'], default='test', help='test = report only, prices.json is not changed')
    ap.add_argument('--scope', choices=['all', 'due'], default='all', help='live only: all tracked ingredients, or only those due this week')
    ap.add_argument('--limit', type=int, default=30, help='test only: how many ingredients to search')
    ap.add_argument('--only', help='comma-separated ingredient ids (overrides everything else)')
    ap.add_argument('--shops', default=','.join(SELLERS), help='which of the Google Shopping shops to use')
    ap.add_argument('--fixtures', help='read saved DataForSEO results from DIR/<id>.json instead of calling the API (for tests)')
    ap.add_argument('--html', default=os.path.join(ROOT, 'index.html'))
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--prices', default=os.path.join(ROOT, 'prices.json'))
    ap.add_argument('--report', default=os.path.join(HERE, 'google-shopping-report.md'))
    ap.add_argument('--raw-dir', default=os.path.join(HERE, 'google-shopping-raw'))
    ap.add_argument('--today')
    ap.add_argument('--wait', type=int, default=1500, help='give up waiting for Google results after this many seconds')
    ap.add_argument('--max-spend', type=float, default=2.0, help='refuse to run if the searches could cost more than this many US dollars')
    a = ap.parse_args()

    U = load_updater()
    cfg = json.load(open(a.config, encoding='utf-8'))
    cat_all = U.read_catalogue(open(a.html, encoding='utf-8').read())
    tracked = [p for p in cat_all if p in cfg['products'] and not cfg['products'][p].get('skip')]
    today = a.today or dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')
    today_d = dt.date.fromisoformat(today)
    shops = [s for s in a.shops.split(',') if s]
    for s in shops:
        if s not in SELLERS:
            sys.exit(f'Unknown shop "{s}". Google Shopping shops: {", ".join(SELLERS)}')

    if a.only:
        ids = [p for p in a.only.split(',') if p]
        bad = [p for p in ids if p not in tracked]
        if bad:
            sys.exit('Not tracked (no rules in config.json, or skipped): ' + ', '.join(bad))
    elif a.mode == 'test':
        first = [p for p in cfg.get('test_products', []) if p in tracked]
        rest = spread([p for p in tracked if p not in first], max(0, a.limit - len(first)))
        ids = (first + rest)[:a.limit]
    elif a.scope == 'due':
        ids = [p for p in tracked if U.is_due(p, cfg['products'][p], today_d)]
    else:
        ids = list(tracked)
    if not ids:
        sys.exit('Nothing to search.')
    est = len(ids) * COST_PER_SEARCH
    print(f'{a.mode} mode: {len(ids)} ingredients, estimated cost about ${est:.2f} (limit ${a.max_spend:.2f})')
    if est > a.max_spend:
        sys.exit('The searches could cost more than the spending limit. Raise --max-spend if that is intended.')

    work = tempfile.mkdtemp(prefix='gshop-')
    fx = os.path.join(work, 'fixtures')
    spent = 0.0
    if a.fixtures:
        results = {}
        for p in ids:
            fp = os.path.join(a.fixtures, p + '.json')
            if os.path.exists(fp):
                results[p] = {'result': json.load(open(fp, encoding='utf-8'))}
    else:
        login, password = os.environ.get('DATAFORSEO_LOGIN', ''), os.environ.get('DATAFORSEO_PASSWORD', '')
        if not login or not password:
            sys.exit('DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD are not set. In GitHub: Settings > Secrets and variables > Actions.')
        queries = {p: cfg['products'][p]['kw'] for p in ids}
        results, spent = search(ids, queries, login, password, a.wait, a.max_spend, a.raw_dir)
    if not results:
        sys.exit('No results came back, so nothing was changed.')
    examples = []
    got = to_fixtures(results, fx, shops, U, examples)

    # a copy of config.json where every Google Shopping shop looks like a normal shop to update_prices.py
    cfg2 = copy.deepcopy(cfg)
    for s in shops:
        sh = cfg2['shops'].setdefault(s, {})
        name, card = SHOP_DEFAULTS[s]
        sh.setdefault('name', name)
        sh.setdefault('card', card)
        sh.setdefault('n', 10)
        sh.setdefault('n_wide', 15)
        sh['actor'] = 'google-shopping'
        sh['per_1000_results'] = 0
        sh.pop('browser', None)
    cfg_path = os.path.join(work, 'config.json')
    json.dump(cfg2, open(cfg_path, 'w', encoding='utf-8'))

    searched = [p for p in ids if p in results]
    cmd = [sys.executable, os.path.join(HERE, 'update_prices.py'), '--mode', 'live' if a.mode == 'live' else 'test',
           '--shops', ','.join(shops), '--only', ','.join(searched), '--fixtures', fx, '--html', a.html, '--config', cfg_path,
           '--prices', a.prices, '--report', a.report, '--today', today, '--workers', '1', '--freq', 'week']
    print('Matching the listings with the usual rules ...')
    r = subprocess.run(cmd, capture_output=True, text=True)
    tail = '\n'.join((r.stdout + r.stderr).strip().splitlines()[-12:])
    print(tail)
    if r.returncode != 0:
        sys.exit('The matching step failed (see above). prices.json was not changed.')

    # tidy the report heading, add the Google Shopping facts, and fix the note shown on the site
    rep = open(a.report, encoding='utf-8').read()
    head = [f'# Fairbasket Google Shopping update: {a.mode} run, {today}', '',
            f'Ingredients searched: {len(ids)}; answered: {len(results)}. Cost about ${spent:.4f}. '
            f'Listings from the six shops were found for: ' + ', '.join(f'{s} {got.get(s, 0)}' for s in shops) + ' ingredients.',
            '(The lines below were produced by the same matching step as the Apify robot, so "Searches ok" means Google answered.)', '']
    body = [ln for ln in rep.split('\n')[1:] if not ln.startswith('Ingredients searched:')]
    if examples:
        body += ['', '## What Google says about listings whose name has no pack size (for checking)', ''] + examples + ['']
    open(a.report, 'w', encoding='utf-8').write('\n'.join(head) + '\n' + '\n'.join(body))
    if a.mode == 'live' and os.path.exists(a.prices):
        d = json.load(open(a.prices, encoding='utf-8'))
        ran = (d.get('run') or {}).get('shops') or {}
        used = [s for s in shops if (ran.get(s) or {}).get('matched')]
        if used and d.get('checked') == today:
            names = [cfg2['shops'][s]['name'] for s in used]
            d['autoNote'] = (', '.join(names[:-1]) + ' and ' + names[-1] if len(names) > 1 else names[0]) + \
                ' prices are checked automatically twice a week from supermarket listings on Google Shopping.'
            text = U.dump_prices(d, {p: None for p in cat_all})
            open(a.prices, 'w', encoding='utf-8').write(text)
    print(f'Report written to {a.report}')
    print(f'Spent about ${spent:.4f} of DataForSEO credit.')


if __name__ == '__main__':
    main()
