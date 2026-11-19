#!/usr/bin/env node
/* Fairbasket: builds one plain, fast page per recipe (recipes/<id>.html), a recipes index, and sitemap.xml.
   It reads the recipe data straight out of index.html, so the pages always match the app.
   Run from the repository root:  node tools/make-recipe-pages.js
   No packages needed. */
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = process.cwd(), SITE = 'https://fairbasket.co.uk';
const src = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');

/* pull a top-level `const NAME = ...;` block out of index.html */
function block(name, open, close){
  const start = src.indexOf('\n' + 'const ' + name + ' = ' + open);
  if (start < 0) throw new Error('Could not find ' + name + ' in index.html');
  const end = src.indexOf('\n' + close + ';', start);
  if (end < 0) throw new Error('Could not find the end of ' + name);
  return src.slice(start + 1, end + 1 + close.length + 1);
}
/* the recipes are stored in several chunks: the first list, then MEALS.push(...) and Object.assign(COOK, {...}) additions */
function stmts(open, close){
  const out = []; let from = 0;
  for (;;){
    const s = src.indexOf('\n' + open, from); if (s < 0) break;
    const e = src.indexOf('\n' + close, s); if (e < 0) throw new Error('Could not find the end of ' + open);
    out.push(src.slice(s + 1, e + 1 + close.length + 1)); from = e;
  }
  return out;
}
const code = [
  "const OIL = 'oil, salt and pepper';",
  block('P', '{', '}'), block('MEALS', '[', ']'), block('COOK', '{', '}'),
  ...stmts('MEALS.push(', ');'), ...stmts('Object.assign(COOK, {', '});'),
  'this.out = {P, MEALS, COOK};'
].join('\n');
const ctx = {}; vm.createContext(ctx); vm.runInContext(code, ctx, {timeout: 5000});
const {P, MEALS, COOK} = ctx.out;

const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const trimNum = x => String(parseFloat(x.toFixed(2)));
function qty(pid, q){
  const p = P[pid];
  if (p.u === 'g') return q >= 1000 ? trimNum(q / 1000) + 'kg' : Math.round(q) + 'g';
  if (p.u === 'ml') return q >= 1000 ? trimNum(q / 1000) + 'L' : Math.round(q) + 'ml';
  if (p.u === 'clove') return q + (q === 1 ? ' clove' : ' cloves');
  return q + ' ' + (p.uw ? (q === 1 ? p.uw[0] : p.uw[1]) : '');
}
/* "Spaghetti" -> "spaghetti", but "Parmigiano Reggiano" keeps its capitals */
const lowerFirst = s => /\s.*[A-Z]/.test(s) ? s : s.charAt(0).toLowerCase() + s.slice(1);
function ingredientLine(pid, q){
  const p = P[pid], n = p.n.replace(/\s*\([^)]*\)\s*/g, ' ').trim();
  if (p.u === 'g' || p.u === 'ml') return qty(pid, q) + ' ' + lowerFirst(n);
  if (p.u === 'clove') return qty(pid, q) + ' ' + lowerFirst(n);
  /* counted items: "4 baking potatoes", "1 onion", but "1 loaf sliced bread" when the name does not already hold the unit word */
  const uw = p.uw || ['', ''], low = lowerFirst(n), lc = low.toLowerCase();
  const re = w => new RegExp('\\b' + w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$', 'i');   /* the unit word as the last word of the name */
  if (uw[0] && lc.includes(uw[0].toLowerCase())){
    /* the name already holds the unit ("ciabatta loaf", "pitta breads"): only fix the singular and plural at the end */
    let name = low;
    if (q === 1 && uw[1] && re(uw[1]).test(name)) name = name.replace(re(uw[1]), uw[0]);
    else if (q !== 1 && uw[1] && re(uw[0]).test(name) && !re(uw[1]).test(name)) name = name.replace(re(uw[0]), uw[1]);
    return q + ' ' + name;
  }
  return q + ' ' + (q === 1 ? uw[0] : uw[1]) + ' ' + low;
}
const isMeatFish = pid => P[pid] && (P[pid].c === 'meat' || P[pid].c === 'fish');
const isVeg = m => !m.items.some(([pid]) => isMeatFish(pid));

const CSS = `*{box-sizing:border-box}body{margin:0;font:17px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1f2a22;background:#FBF8EE}
a{color:#14561F}header,main,footer{max-width:760px;margin:0 auto;padding:0 18px}
header{display:flex;justify-content:space-between;align-items:center;padding-top:14px;padding-bottom:14px}
.logo{font-weight:800;font-size:1.25rem;text-decoration:none;color:#283F30}
h1{font-size:2rem;line-height:1.2;margin:.4em 0 .2em}h2{font-size:1.3rem;margin:1.6em 0 .4em}
.meta{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 0;padding:0;list-style:none}
.meta li{background:#E6EDDD;border-radius:999px;padding:4px 14px;font-size:.95rem}
ul.ing{padding-left:1.2em}ul.ing li{margin:.25em 0}ol.steps{padding-left:1.3em}ol.steps li{margin:.7em 0}
.cta{background:#283F30;color:#fff;border-radius:16px;padding:18px 20px;margin:26px 0}
.cta p{margin:0 0 10px}.cta a.btn{display:inline-block;background:#F4C542;color:#1f2a22;font-weight:700;text-decoration:none;border-radius:999px;padding:12px 22px;min-height:44px}
.note{color:#4b5a4f;font-size:.95rem}.related{display:flex;flex-wrap:wrap;gap:8px 18px;padding:0;list-style:none}
footer{padding-top:24px;padding-bottom:40px;border-top:1px solid #E3E0D0;margin-top:30px;font-size:.92rem;color:#4b5a4f}
.grid{columns:2 280px;column-gap:28px;padding:0;list-style:none}.grid li{margin:.35em 0;break-inside:avoid}`;

function shell({title, desc, url, body, ld}){
  return `<!doctype html>
<html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title>
<meta name="description" content="${esc(desc)}">
<link rel="canonical" href="${url}">
<link rel="icon" href="/favicon.ico" sizes="any"><link rel="manifest" href="/site.webmanifest">
<meta name="theme-color" content="#283F30">
<meta property="og:type" content="article"><meta property="og:site_name" content="Fairbasket"><meta property="og:locale" content="en_GB">
<meta property="og:title" content="${esc(title)}"><meta property="og:description" content="${esc(desc)}"><meta property="og:url" content="${url}">
<meta property="og:image" content="${SITE}/og-image.png">
<meta name="twitter:card" content="summary_large_image">
${ld ? '<script type="application/ld+json">' + JSON.stringify(ld).replace(/</g, '\\u003c') + '</script>' : ''}
<style>${CSS}</style></head><body>
<header><a class="logo" href="/">Fairbasket</a><a href="/recipes/">All recipes</a></header>
<main>${body}</main>
<footer><p>Fairbasket compares everyday recipes across UK supermarkets. <a href="/#about">About</a> · <a href="/#privacy">Privacy</a> · <a href="/#terms">Terms</a> · <a href="mailto:contact@fairbasket.co.uk">contact@fairbasket.co.uk</a></p></footer>
</body></html>
`;
}

const made = [], skipped = [], changed = new Set();
function put(rel, text){
  const f = path.join(ROOT, rel); let old = null; try { old = fs.readFileSync(f, 'utf8'); } catch (e) {}
  if (old !== text){ fs.writeFileSync(f, text); changed.add(rel); }
}
const byId = Object.fromEntries(MEALS.map(m => [m.id, m]));
function related(m){
  const mine = new Set(m.items.map(([pid]) => pid).filter(pid => P[pid] && P[pid].core));
  return MEALS.filter(o => o.id !== m.id && COOK[o.id] && COOK[o.id].steps).map(o => ({o, s: o.items.filter(([pid]) => mine.has(pid)).length}))
    .filter(x => x.s > 0).sort((a, b) => b.s - a.s || a.o.n.localeCompare(b.o.n)).slice(0, 6).map(x => x.o);
}
fs.mkdirSync(path.join(ROOT, 'recipes'), {recursive: true});
for (const m of MEALS){
  const c = COOK[m.id];
  if (m.custom || !c || !Array.isArray(c.steps) || !c.steps.length || !m.items.length){ skipped.push(m.id); continue; }
  const url = SITE + '/recipes/' + m.id + '.html';
  const ings = m.items.filter(([pid]) => P[pid]).map(([pid, q]) => ingredientLine(pid, q));
  const steps = c.steps.map(s => Array.isArray(s) ? s[1] : s);
  const pantry = c.pantry ? String(c.pantry) : '';
  const tags = []; if (isVeg(m)) tags.push('Vegetarian');
  const desc = `${m.n}: a ${c.t}-minute recipe for ${m.serves}, with ingredients, step-by-step method, and a link to see which UK supermarket is cheapest for it.`;
  const rel = related(m);
  const body = `<p class="note"><a href="/recipes/">Recipes</a> › ${esc(m.n)}</p>
<h1>${esc(m.n)}</h1>
<ul class="meta"><li>${c.t} minutes</li><li>Serves ${m.serves}</li><li>${ings.length} ingredients</li>${tags.map(t => '<li>' + t + '</li>').join('')}</ul>
<div class="cta"><p><strong>Which shop is cheapest for this?</strong> Fairbasket prices this recipe at Tesco, Asda, Morrisons, Aldi, Lidl and more, and shows which prices were checked and which are estimates.</p><a class="btn" href="/#/recipe/${encodeURIComponent(m.id)}">Compare prices for this recipe</a></div>
<h2>Ingredients</h2>
<ul class="ing">${ings.map(i => '<li>' + esc(i) + '</li>').join('')}</ul>
${pantry ? '<p class="note">From your cupboard: ' + esc(pantry) + '.</p>' : ''}
${c.kit ? '<h2>You will need</h2><p>' + esc(c.kit.charAt(0).toUpperCase() + c.kit.slice(1)) + '.</p>' : ''}
<h2>Method</h2>
<ol class="steps">${steps.map(s => '<li>' + esc(s) + '</li>').join('')}</ol>
${rel.length ? '<h2>More recipes like this</h2><ul class="related">' + rel.map(o => '<li><a href="/recipes/' + o.id + '.html">' + esc(o.n) + '</a></li>').join('') + '</ul>' : ''}
<p class="note">Quantities are for ${m.serves} people. Prices change, so open the recipe in Fairbasket for the latest.</p>`;
  const ld = {'@context': 'https://schema.org', '@type': 'Recipe', name: m.n, description: desc, url,
    recipeYield: m.serves + ' servings', totalTime: 'PT' + c.t + 'M', recipeCuisine: 'British',
    recipeIngredient: ings, recipeInstructions: steps.map((t, i) => ({'@type': 'HowToStep', position: i + 1, text: t})),
    author: {'@type': 'Organization', name: 'Fairbasket', url: SITE}};
  if (isVeg(m)) ld.suitableForDiet = 'https://schema.org/VegetarianDiet';
  put('recipes/' + m.id + '.html', shell({title: m.n + ' recipe: ingredients and method | Fairbasket', desc, url, body, ld}));
  made.push(m);
}
/* index page */
made.sort((a, b) => a.n.localeCompare(b.n));
const idxBody = `<h1>Recipes</h1><p>${made.length} everyday recipes. Open any of them in Fairbasket to see which UK supermarket is cheapest for the ingredients.</p>
<ul class="grid">${made.map(m => `<li><a href="/recipes/${m.id}.html">${esc(m.n)}</a> <span class="note">${COOK[m.id].t} min</span></li>`).join('')}</ul>`;
put('recipes/index.html', shell({title: 'Easy budget recipes for UK supermarkets | Fairbasket',
  desc: `${made.length} everyday recipes with ingredients and methods, and a price comparison across UK supermarkets.`, url: SITE + '/recipes/', body: idxBody}));

/* sitemap: the home page, the recipes index and every recipe. A page keeps its old date unless its content changed. */
const today = process.env.SITEMAP_DATE || new Date().toISOString().slice(0, 10);
let oldMap = ''; try { oldMap = fs.readFileSync(path.join(ROOT, 'sitemap.xml'), 'utf8'); } catch (e) {}
const oldDate = {}; for (const m of oldMap.matchAll(/<loc>([^<]+)<\/loc>\s*<lastmod>([^<]+)<\/lastmod>/g)) oldDate[m[1]] = m[2];
const pages = [[SITE + '/', 'index.html'], [SITE + '/recipes/', 'recipes/index.html']].concat(made.map(m => [SITE + '/recipes/' + m.id + '.html', 'recipes/' + m.id + '.html']));
const homeChanged = !!process.env.HOME_CHANGED;   /* the workflow sets this when index.html itself changed */
put('sitemap.xml', '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
  pages.map(([u, rel]) => `  <url>\n    <loc>${u}</loc>\n    <lastmod>${(changed.has(rel) || (rel === 'index.html' && homeChanged) || !oldDate[u]) ? today : oldDate[u]}</lastmod>\n  </url>`).join('\n') + '\n</urlset>\n');
console.log('Made ' + made.length + ' recipe pages, an index page and a sitemap with ' + pages.length + ' addresses. Files changed this run: ' + changed.size + '.' + (skipped.length ? ' Skipped (no method yet): ' + skipped.length : ''));
