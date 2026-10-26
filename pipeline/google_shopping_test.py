#!/usr/bin/env python3
"""
Fairbasket: Google Shopping test (DataForSEO).

What it does, in plain words
  1. Takes about 20 everyday ingredients (their search words come from pipeline/config.json).
  2. Asks DataForSEO to run a UK Google Shopping search for each one (this is a paid API, about a tenth of a US cent per search).
  3. Looks at who is selling each product and at what price.
  4. Writes a short report that answers ONE question: which of the 8 supermarkets show up with real prices?

It never touches prices.json or the website. It only reads, and it stops by itself if the account runs low on credit.

Needs only Python 3.9+ (standard library). The login and password are read from the environment variables
DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD (GitHub secrets) and are never printed or saved.

Usage
  python pipeline/google_shopping_test.py                 real run, about 20 searches
  python pipeline/google_shopping_test.py --sandbox       free dummy data, only checks the plumbing works
  python pipeline/google_shopping_test.py --count 5       a smaller run
"""
import argparse, base64, collections, datetime as dt, json, os, re, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

# the ingredients to test: a mix of meat, veg, tins, dairy, bakery and cupboard items
TEST_IDS = ['beef_mince', 'spaghetti', 'chopped_tomatoes', 'eggs', 'milk', 'cheddar', 'onion', 'butter',
            'chicken_breast', 'rice', 'baked_beans', 'bread', 'bananas', 'carrots', 'frozen_peas', 'oats',
            'plain_flour', 'olive_oil', 'ketchup', 'sausages', 'tuna', 'yoghurt', 'potatoes', 'salmon']

SHOPS = {   # shop id -> (display name, words that identify it in a seller name or web address)
    'tesco': ('Tesco', r'tesco'),
    'asda': ('Asda', r'asda'),
    'sainsburys': ("Sainsbury's", r'sainsbury'),
    'morrisons': ('Morrisons', r'morrisons'),
    'aldi': ('Aldi', r'\baldi\b'),
    'lidl': ('Lidl', r'\blidl\b'),
    'waitrose': ('Waitrose', r'waitrose'),
    'iceland': ('Iceland', r'\biceland\b'),
}
OTHER_KNOWN = {'ocado': r'ocado', 'amazon': r'amazon', 'booths': r'booths', 'co-op': r'\bco-?op\b'}


def call(base, path, login, password, body=None, timeout=60):
    url = base.rstrip('/') + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method='POST' if body is not None else 'GET', headers={
        'Authorization': 'Basic ' + base64.b64encode(f'{login}:{password}'.encode()).decode(),
        'Content-Type': 'application/json', 'User-Agent': 'fairbasket-google-shopping-test'})
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
    hay = ' '.join(str(listing.get(k) or '') for k in ('seller', 'domain', 'shop_name', 'url', 'shopping_url')).lower()
    for sid, (_n, rx) in SHOPS.items():
        if re.search(rx, hay):
            return sid
    return None


def money(v):
    return f'£{v:.2f}' if isinstance(v, (int, float)) else 'no price'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sandbox', action='store_true', help='use DataForSEO\'s free dummy-data server (checks the plumbing only)')
    ap.add_argument('--count', type=int, default=20, help='how many ingredients to search (max 25)')
    ap.add_argument('--config', default=os.path.join(HERE, 'config.json'))
    ap.add_argument('--out', default=os.path.join(HERE, 'google-shopping-test'))
    ap.add_argument('--wait', type=int, default=900, help='give up waiting for results after this many seconds')
    ap.add_argument('--budget', type=float, default=0.50, help='stop if the searches would cost more than this many US dollars')
    a = ap.parse_args()

    login, password = os.environ.get('DATAFORSEO_LOGIN', ''), os.environ.get('DATAFORSEO_PASSWORD', '')
    if not login or not password:
        sys.exit('DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD are not set. In GitHub: Settings > Secrets and variables > Actions > New repository secret.')
    base = os.environ.get('DATAFORSEO_BASE') or ('https://sandbox.dataforseo.com' if a.sandbox else 'https://api.dataforseo.com')

    cfg = json.load(open(a.config, encoding='utf-8')) if os.path.exists(a.config) else {'products': {}}
    ids = [p for p in TEST_IDS if p in cfg.get('products', {})][:max(1, min(a.count, 25))]
    if not ids:
        sys.exit('None of the test ingredients were found in config.json.')
    queries = {pid: cfg['products'][pid]['kw'] for pid in ids}

    # a search costs well under one cent, so this is only a safety net against a typo making it run away
    if len(queries) * 0.002 > a.budget:
        sys.exit('Too many searches for the safety budget.')

    tasks = [{'keyword': kw, 'location_code': 2826, 'language_code': 'en', 'depth': 40, 'tag': pid} for pid, kw in queries.items()]
    print(f'Posting {len(tasks)} Google Shopping searches to {base} ...')
    posted = call(base, '/v3/merchant/google/products/task_post', login, password, tasks)
    if posted.get('status_code') not in (20000, None) and not posted.get('tasks'):
        sys.exit('DataForSEO said: %s %s' % (posted.get('status_code'), posted.get('status_message')))
    pending, spent = {}, float(posted.get('cost') or 0)
    for t in posted.get('tasks', []):
        if t.get('status_code') == 20100 and t.get('id'):
            pending[t['id']] = (t.get('data') or {}).get('tag') or ''
        else:
            code, msg = t.get('status_code'), t.get('status_message')
            print(f'  one search was not accepted: {code} {msg}')
            if code in (40200, 40201):
                sys.exit('DataForSEO says the account is out of credit or not verified yet (%s %s). Open the DataForSEO dashboard and finish verifying the account.' % (code, msg))
    if not pending:
        sys.exit('No search was accepted, so nothing to wait for.')
    print(f'{len(pending)} searches accepted (cost so far about ${spent:.4f}). Waiting for Google results ...')

    results, started = {}, time.time()
    while pending and time.time() - started < a.wait:
        time.sleep(15)
        for tid in list(pending):
            try:
                r = call(base, f'/v3/merchant/google/products/task_get/advanced/{tid}', login, password)
            except RuntimeError as e:
                print('  retrying later:', str(e)[:100])
                continue
            for t in r.get('tasks', []):
                code = t.get('status_code')
                if code == 20000:
                    results[pending.pop(tid)] = t
                elif code in (40601, 40602):      # handed to a worker / still in the queue
                    pass
                else:
                    print(f'  {pending[tid]}: failed ({code} {t.get("status_message")})')
                    pending.pop(tid)
        print(f'  {len(results)} done, {len(pending)} still waiting ({int(time.time() - started)}s)')
    if pending:
        print(f'Gave up on {len(pending)} searches that did not finish in {a.wait}s: {", ".join(pending.values())}')
    if not results:
        sys.exit('No results came back.')

    # ---- analyse
    os.makedirs(a.out, exist_ok=True)
    per_shop = {s: {'queries': set(), 'priced': 0, 'listings': 0, 'examples': []} for s in SHOPS}
    sellers = collections.Counter()
    price_seen = total = 0
    for pid, t in results.items():
        spent += float(t.get('cost') or 0)
        with open(os.path.join(a.out, pid + '.json'), 'w', encoding='utf-8') as f:
            json.dump(t.get('result'), f, indent=1, ensure_ascii=False)
        found = []
        walk(t.get('result'), found)
        seen_keys = set()
        for it in found:
            key = (str(it.get('title')), str(it.get('seller') or it.get('domain')), it.get('price'))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            total += 1
            price_seen += isinstance(it.get('price'), (int, float))
            who = (it.get('seller') or it.get('domain') or '?')
            sellers[str(who).strip()] += 1
            sid = which_shop(it)
            if sid:
                s = per_shop[sid]
                s['queries'].add(pid)
                s['listings'] += 1
                s['priced'] += isinstance(it.get('price'), (int, float))
                if len(s['examples']) < 4:
                    s['examples'].append((queries[pid], str(it.get('title'))[:70], it.get('price')))

    today = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')
    n_q = len(results)
    L = [f'# Google Shopping test, {today}' + (' (SANDBOX: dummy data, ignore the shops)' if a.sandbox else ''), '',
         f'Searches finished: {n_q} of {len(queries)}. Listings seen: {total}, of which {price_seen} had a price. '
         f'Cost of this test: about ${spent:.4f}.', '',
         '## Which supermarkets showed up?', '',
         '| Shop | Searches it appeared in | Listings | Listings with a price | Verdict |', '|---|---|---|---|---|']
    covered = []
    for sid, (name, _rx) in SHOPS.items():
        s = per_shop[sid]
        share = len(s['queries']) / n_q if n_q else 0
        if share >= 0.5 and s['priced']:
            verdict = 'GOOD: usable'
            covered.append(name)
        elif s['queries'] and s['priced']:
            verdict = 'partial'
        elif s['queries']:
            verdict = 'listed but no prices'
        else:
            verdict = 'not found'
        L.append(f'| {name} | {len(s["queries"])} of {n_q} | {s["listings"]} | {s["priced"]} | {verdict} |')
    L += ['', f'**Shops that look usable from Google Shopping: {", ".join(covered) if covered else "none"}.**', '',
          '## Examples seen', '']
    for sid, (name, _rx) in SHOPS.items():
        for kw, title, price in per_shop[sid]['examples']:
            L.append(f'- {name}, searching "{kw}": {title}, {money(price)}')
    L += ['', '## Every seller seen (top 30)', '', 'If a supermarket you expected is missing above, check here in case it uses a different name.', '']
    L += [f'- {name}: {n}' for name, n in sellers.most_common(30)]
    L += ['', '_Raw results are saved next to this report (one file per ingredient) so they can be inspected._', '']
    with open(os.path.join(a.out, 'report.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(L))
    print('\n'.join(L))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write('\n'.join(L) + '\n')


if __name__ == '__main__':
    main()
