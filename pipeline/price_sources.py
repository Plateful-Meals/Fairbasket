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


def merge_into(theirs, ours, base, shops):
    """Put what THIS run changed (ours compared with base, the prices.json the run started from) into the newest
    prices.json on GitHub (theirs). Everything the run did not change stays as it is on GitHub, so prices saved in the
    meantime by the home checker or typed into the price editor are never lost."""
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
                row.pop(s, None)
            else:
                row[s] = mine
            if not row:
                out['prices'].pop(pid, None)
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
    else:
        sys.exit('usage: price_sources.py merge THEIRS.json OURS.json BASE.json shop1,shop2')
