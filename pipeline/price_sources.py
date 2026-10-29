#!/usr/bin/env python3
"""
Fairbasket: shared parts of the automatic price sources (Google Shopping, Aldi product pages).

Each source collects product listings, saves them in the same shape as the Apify robot's results, and then lets
pipeline/update_prices.py do the matching (right product, right pack size, sanity checks). This file holds what the
sources share:

  run_matcher()   runs update_prices.py on the saved listings
  guard()         a last check on every price a run wants to change. A price is NOT changed, and is listed in the
                  report for a person to look at, when
                    - it jumps up by more than half compared with the price it replaces, or falls by more than 40%
                      (same product) / more than 65% (a different, cheaper product), or
                    - per gram / per item it is more than 2.2 times the middle price of the other shops for that product
                      (or under a third of it).
                  The old price stays (with its old date), so a bad listing can never push a silly price onto the site.
  shop_names()    display names for reports.
  SOURCES, tag_source(), respect_priority(), expire(), merge_into()
                  combining sources: every price says where it came from ("src"); a better source's price is never
                  replaced while it is fresh (hand-typed 14 days, Pepesto 3 days, others 7 days), and Pepesto prices
                  are removed after 3 days as its terms require.
  ProductMap      pipeline/product-map.json: blocked shop products and the products matched before, so new or changed
                  matches can be listed for a person to check once.
"""
import copy, datetime as dt, importlib.util, json, os, re, statistics, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SHOP_DEFAULTS = {
    'tesco': ('Tesco', 'Clubcard'), 'sainsburys': ("Sainsbury's", 'Nectar'), 'asda': ('Asda', 'Asda Rewards'),
    'morrisons': ('Morrisons', 'More Card'), 'aldi': ('Aldi', ''), 'lidl': ('Lidl', 'Lidl Plus'),
    'waitrose': ('Waitrose', 'myWaitrose'), 'iceland': ('Iceland', 'Bonus Card'),
}
MAX_RISE, MAX_DROP, OUTLIER = 1.5, 0.6, 2.2


def load_updater():
    spec = importlib.util.spec_from_file_location('update_prices', os.path.join(HERE, 'update_prices.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def shop_names(cfg):
    return {s: (cfg.get('shops', {}).get(s, {}).get('name') or SHOP_DEFAULTS.get(s, (s,))[0]) for s in SHOP_DEFAULTS}


def matcher_config(cfg, shops, source):
    """A copy of config.json in which each shop of this source looks like an ordinary shop to update_prices.py."""
    cfg2 = copy.deepcopy(cfg)
    for s in shops:
        sh = cfg2['shops'].setdefault(s, {})
        name, card = SHOP_DEFAULTS.get(s, (s, ''))
        sh.setdefault('name', name)
        if card:
            sh.setdefault('card', card)
        sh.setdefault('n', 10)
        sh.setdefault('n_wide', 15)
        sh['actor'] = source
        sh['per_1000_results'] = 0
        sh.pop('browser', None)
    return cfg2


def run_matcher(cfg2, workdir, fixtures, shops, ids, html, prices_in, prices_out, report, today, mode):
    """Run update_prices.py on saved listings. prices_in is never changed; the result goes to prices_out."""
    cfg_path = os.path.join(workdir, 'config-' + '-'.join(shops) + '.json')
    with open(cfg_path, 'w', encoding='utf-8') as f:
        json.dump(cfg2, f)
    if os.path.exists(prices_in):
        with open(prices_in, 'rb') as a, open(prices_out, 'wb') as b:
            b.write(a.read())
    cmd = [sys.executable, os.path.join(HERE, 'update_prices.py'), '--mode', 'live' if mode == 'live' else 'test',
           '--shops', ','.join(shops), '--only', ','.join(ids), '--fixtures', fixtures, '--html', html, '--config', cfg_path,
           '--prices', prices_out, '--report', report, '--today', today, '--workers', '1', '--freq', 'week']
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr)


def _per_unit(entry):
    """Cheapest regular shelf price per gram / ml / item over the packs of one entry."""
    vals = [p[1] / p[0] for p in entry.get('packs', []) if isinstance(p, list) and len(p) > 1 and p[0] and p[1]]
    return min(vals) if vals else None


def _plain(name):
    return re.sub(r'[^a-z0-9]', '', str(name or '').lower())


def guard(old, new, shops, today, names, label=None):
    """Compare the prices this run wants to write (new) with what was there before (old). Puts the old entry back for
    any change that fails the checks. Returns report lines (empty if nothing was held back)."""
    held = []
    for pid, by_shop in list(new.get('prices', {}).items()):
        for s in shops:
            n = by_shop.get(s)
            if not n or n.get('checked') != today:
                continue
            o = old.get('prices', {}).get(pid, {}).get(s)
            if o == n:
                continue
            why = []
            if o:
                before = {p[0]: p[1] for p in o.get('packs', []) if len(p) > 1}
                # a cheaper DIFFERENT product (e.g. the shop's value range) may well be much cheaper; the same product
                # suddenly costing 40% less is more likely a misread
                same = _plain(o.get('product')) == _plain(n.get('product'))
                floor = MAX_DROP if same else 0.35
                for p in n.get('packs', []):
                    if p[0] in before and before[p[0]]:
                        r = p[1] / before[p[0]]
                        if r > MAX_RISE or r < floor:
                            why.append(f'{p[0]}: £{before[p[0]]:.2f} -> £{p[1]:.2f} ({(r - 1) * 100:+.0f}%)')
            mine = _per_unit(n)
            others = [u for sid, e in new['prices'].get(pid, {}).items() if sid != s for u in [_per_unit(e)] if u]
            if mine and len(others) >= 2:
                mid = statistics.median(others)
                if mine > OUTLIER * mid:
                    why.append(f'{mine / mid:.1f}x the middle price of the other shops')
                elif mine < mid / 3:
                    why.append(f'under a third of the middle price of the other shops')
            if why:
                if o:
                    by_shop[s] = o
                else:
                    del by_shop[s]
                held.append(f'- {names.get(s, s)}, {pid}: "{n.get("product", "")}" held back ({"; ".join(why)}). '
                            + ('Old price kept.' if o else 'Not added.'))
        if not by_shop:
            del new['prices'][pid]
    if not held:
        return []
    return ['## Held back for a person to check', '',
            'These prices were found but look wrong (a big jump, or far from the other shops), so the site keeps the old '
            'price. If one is right, type it in with the price editor.', ''] + held + ['']


def keep_recent_packs(old, new, shops, today, max_age=14):
    """A run may find only some pack sizes of a product (Google rarely lists them all). Pack sizes it did not find are
    kept from the old entry when that entry is at most max_age days old, so a fresh 220g price does not wipe out a
    still-recent 400g price."""
    t = dt.date.fromisoformat(today)
    kept = 0
    for pid, by_shop in new.get('prices', {}).items():
        for s in shops:
            n, o = by_shop.get(s), old.get('prices', {}).get(pid, {}).get(s)
            if not n or not o or n is o or n.get('checked') != today or o.get('checked') == today:
                continue
            try:
                age = (t - dt.date.fromisoformat(o.get('checked') or '')).days
            except ValueError:
                continue
            if age > max_age:
                continue
            have = {p[0] for p in n.get('packs', [])}
            pairs = list(zip(n.get('packs', []), n.get('items', [{}] * len(n.get('packs', [])))))
            for pk, it in zip(o.get('packs', []), o.get('items', [{}] * len(o.get('packs', [])))):
                if pk[0] not in have and all(abs(pk[0] - h) / h > 0.15 for h in have):
                    pairs.append((pk, it))
                    kept += 1
            pairs.sort(key=lambda x: x[0][0])
            n['packs'] = [x[0] for x in pairs]
            if n.get('items'):
                n['items'] = [x[1] for x in pairs]
    return kept


def finish(U, cat_all, prices_path, new):
    """Write prices.json in the usual layout (one shop per line)."""
    text = U.dump_prices(new, {p: None for p in cat_all})
    with open(prices_path, 'w', encoding='utf-8') as f:
        f.write(text)


def today_utc():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d')


# ----------------------------------------------------------------------------- combining sources
# Every price in prices.json says where it came from ("src"). When two sources have a price for the same product at
# the same shop, the better source wins while its price is fresh; after that any newer price may replace it.
#   rank  = how much the source is trusted (higher wins)
#   fresh = days a price from it keeps priority over lower-ranked sources
#   max_age = days after which the price is removed altogether (None = kept until replaced; the site's own 42-day
#             rule still applies). Pepesto's terms allow keeping its prices for 3 days.
SOURCES = {
    'manual':     {'rank': 50, 'fresh': 14, 'max_age': None, 'label': 'typed in by hand (price editor)'},
    'pepesto':    {'rank': 40, 'fresh': 3,  'max_age': 3,    'label': 'Pepesto (shop website data, daily)'},
    'aldi-pages': {'rank': 35, 'fresh': 7,  'max_age': None, 'label': "Aldi's own product pages"},
    'checker':    {'rank': 30, 'fresh': 7,  'max_age': None, 'label': 'home price checker'},
    'google':     {'rank': 20, 'fresh': 7,  'max_age': None, 'label': 'Google Shopping'},
    'unknown':    {'rank': 10, 'fresh': 0,  'max_age': None, 'label': 'older price, source not recorded'},
}
MANUAL_NOTE = 'Price typed in by hand'


def source_of(entry):
    if not isinstance(entry, dict):
        return 'unknown'
    s = entry.get('src')
    if s in SOURCES:
        return s
    if str(entry.get('note') or '').startswith(MANUAL_NOTE):
        return 'manual'
    return 'unknown'


def _age(entry, today):
    try:
        return (dt.date.fromisoformat(today) - dt.date.fromisoformat(entry.get('checked') or '')).days
    except (ValueError, TypeError):
        return 999


def tag_source(new, old, shops, today, src):
    """Mark the entries this run wrote today with their source."""
    n = 0
    for pid, by_shop in new.get('prices', {}).items():
        for s in shops:
            e = by_shop.get(s)
            if e and e.get('checked') == today and e != old.get('prices', {}).get(pid, {}).get(s):
                e['src'] = src
                n += 1
    return n


def wins(current, incoming, today):
    """Should incoming replace current? Only a fresher-or-equal price, and never over a better source that is still
    fresh."""
    if not current:
        return True
    if not incoming:
        return False
    a, b = SOURCES[source_of(current)], SOURCES[source_of(incoming)]
    if a['rank'] > b['rank'] and _age(current, today) <= a['fresh']:
        return False
    return (incoming.get('checked') or '') >= (current.get('checked') or '')


def respect_priority(old, new, shops, today, names=None):
    """Undo changes in new that would push out a better, still-fresh price from another source. Returns report lines."""
    kept = []
    for pid, by_shop in list(new.get('prices', {}).items()):
        for s in shops:
            o, n = old.get('prices', {}).get(pid, {}).get(s), by_shop.get(s)
            if n is None or n == o or not o:
                continue
            if not wins(o, n, today):
                by_shop[s] = o
                kept.append(f'- {(names or {}).get(s, s)}, {pid}: kept the {SOURCES[source_of(o)]["label"]} price from '
                            f'{o.get("checked")}')
    if not kept:
        return []
    return ['## Kept because a better source has a fresh price', ''] + kept + ['']


def expire(d, today):
    """Remove prices older than their source allows (Pepesto: 3 days). Returns how many were removed."""
    gone = 0
    for pid, by_shop in list(d.get('prices', {}).items()):
        for s, e in list(by_shop.items()):
            mx = SOURCES[source_of(e)]['max_age']
            if mx is not None and _age(e, today) > mx:
                del by_shop[s]
                gone += 1
        if not by_shop:
            del d['prices'][pid]
    return gone


# ----------------------------------------------------------------------------- the product map
class ProductMap:
    """pipeline/product-map.json: for each product and shop, the shop products matched to it before.

      {"beef_mince": {"tesco": {"pins": {"500": {"url": ..., "name": ..., "gtin": [...], "first": d, "last": d,
                                                   "runs": 3}},
                                "block": ["https://...wrong product..."]}}}

    - "block": shop products a person has said are wrong for this product. They are never used again.
    - "pins": the product matched at each pack size, with how many runs in a row picked it. A new match, or a
      different product replacing an earlier one, is listed in the report so a person can check it once; if it is
      wrong, its link goes into "block".
    """

    def __init__(self, path):
        self.path = path
        self.data = {}
        if os.path.exists(path):
            try:
                self.data = json.load(open(path, encoding='utf-8'))
            except ValueError:
                self.data = {}
        self.new, self.changed = [], []

    def blocked(self, pid, shop):
        return set(self.data.get(pid, {}).get(shop, {}).get('block', []))

    def record(self, new, old, shops, today, gtins=None, names=None):
        gtins = gtins or {}
        for pid, by_shop in new.get('prices', {}).items():
            for s in shops:
                e = by_shop.get(s)
                if not e or e.get('checked') != today or e == old.get('prices', {}).get(pid, {}).get(s):
                    continue
                node = self.data.setdefault(pid, {}).setdefault(s, {})
                pins = node.setdefault('pins', {})
                for pk, it in zip(e.get('packs', []), e.get('items', [])):
                    key, url = f'{pk[0]:g}', it.get('u') or ''
                    if not url:
                        continue
                    p = pins.get(key)
                    if p and p.get('url') == url:
                        p['runs'] = int(p.get('runs', 1)) + 1
                        p['last'] = today
                        continue
                    line = f'- {(names or {}).get(s, s)}, {pid} ({key}): [{it.get("n", "")}]({url})'
                    if p:
                        self.changed.append(line + f' replaces [{p.get("name", "")}]({p.get("url")})')
                    else:
                        self.new.append(line)
                    pins[key] = {'url': url, 'name': it.get('n', ''), 'first': today, 'last': today, 'runs': 1}
                    if gtins.get(url):
                        pins[key]['gtin'] = gtins[url]

    def report(self):
        out = []
        if self.changed:
            out += ['## Matches that changed (check these)', '',
                    'A different shop product was picked than last time (usually a cheaper own-label pack). If one is '
                    'wrong, copy its link into "block" for that product and shop in pipeline/product-map.json.', ''] \
                   + self.changed + ['']
        if self.new:
            out += [f'## New matches ({len(self.new)})', '',
                    'First time these were matched. Check a few; a wrong one goes into "block" in pipeline/product-map.json.',
                    ''] + self.new[:400] + ([f'- ... and {len(self.new) - 400} more'] if len(self.new) > 400 else []) + ['']
        return out

    def save(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(dict(sorted(self.data.items())), f, ensure_ascii=False, indent=1)


def merge_into(theirs, ours, base, shops, today=None):
    """Put what THIS run changed (ours compared with base, the prices.json the run started from) into the newest
    prices.json on GitHub (theirs). Everything the run did not change stays as it is on GitHub, so prices saved in the
    meantime by the home checker or typed into the price editor are never lost. A change never pushes out a price
    from a better source that is still fresh (see SOURCES), and prices past their source's limit are removed."""
    today = today or today_utc()
    out = copy.deepcopy(theirs)
    out.setdefault('prices', {})
    for pid in set(ours.get('prices', {})) | set(base.get('prices', {})):
        for s in shops:
            mine = ours.get('prices', {}).get(pid, {}).get(s)
            before = base.get('prices', {}).get(pid, {}).get(s)
            if mine == before:
                continue                                   # this run did not touch it
            row = out['prices'].setdefault(pid, {})
            if mine is None:
                if row.get(s) == before:                   # only remove what this run saw; a newer price stays
                    row.pop(s, None)
            elif wins(row.get(s), mine, today):
                row[s] = mine
            if not row:
                out['prices'].pop(pid, None)
    expire(out, today)
    if (ours.get('checked') or '') > (out.get('checked') or ''):
        for k in ('checked', 'label'):
            if k in ours:
                out[k] = ours[k]
    return out


if __name__ == '__main__':
    # python pipeline/price_sources.py merge THEIRS.json OURS.json BASE.json shop1,shop2   -> rewrites THEIRS.json
    if len(sys.argv) == 6 and sys.argv[1] == 'merge':
        U = load_updater()
        load = lambda p: json.load(open(p, encoding='utf-8'))
        merged = merge_into(load(sys.argv[2]), load(sys.argv[3]), load(sys.argv[4]), [s for s in sys.argv[5].split(',') if s])
        cat = U.read_catalogue(open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read())
        finish(U, cat, sys.argv[2], merged)
        print('Merged this run\'s prices into the newest prices.json.')
    elif len(sys.argv) == 3 and sys.argv[1] == 'expire':
        # python pipeline/price_sources.py expire prices.json   -> removes prices older than their source allows
        U = load_updater()
        d = json.load(open(sys.argv[2], encoding='utf-8'))
        gone = expire(d, today_utc())
        if gone:
            cat = U.read_catalogue(open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read())
            finish(U, cat, sys.argv[2], d)
        print(f'Removed {gone} price(s) older than their source allows.')
    else:
        sys.exit('usage: price_sources.py merge THEIRS.json OURS.json BASE.json shop1,shop2\n'
                 '       price_sources.py expire prices.json')
