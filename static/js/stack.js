/* Stapel splitsen: the 42-page sample scan falls apart into dossiers and documents.
   The outcome is not drawn by hand: it comes from /api/voorbeeld.json, the same data the werkbank shows.
   What the visitor already did in the werkbank (a split, approvals) is read from the browser's own storage. */
(() => {
'use strict';
const root = document.getElementById('studio');
if (!root) return;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const num = v => (v === null || v === undefined || v === '') ? '' : Number(v).toLocaleString('nl-NL', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const calm = matchMedia('(prefers-reduced-motion:reduce)').matches;

let D = null, O = {}, docs = [], lanes = [], pageDoc = {}, timers = [];
const stat = {blank: 0, cover: 0, docs: 0};

function buildDocs() {
  const out = [];
  for (const d of D.docs) {
    const st = (D.staged || []).find(s => s.id === d.id);
    if (st && O.split && O.split[d.id]) st.parts.forEach(k => out.push(D.alternates.find(a => a.id === k)));
    else if (O.parts && O.parts[d.id]) O.parts[d.id].forEach(k => O.derived && O.derived[k] && out.push(O.derived[k]));
    else out.push(d);
  }
  docs = out.filter(Boolean).map(d => {
    const o = (O.docs || {})[d.id] || {};
    return Object.assign({}, o.server || d, {status: o.status || 'review'});
  });
  pageDoc = {};
  docs.forEach(d => d.pages.forEach(p => { pageDoc[p] = d; }));
  const order = D.pages.filter(p => p.kind === 'cover').map(p => p.ref);
  lanes = order.map((nr, i) => ({nr, i, client: D.clients[nr] || {nr, name: 'Dossier ' + nr}, docs: docs.filter(d => d.client_nr === nr)}));
}

const cell = n => `--col:${(n - 1) % 14};--row:${Math.floor((n - 1) / 14)}`;
const laneOf = n => { let cur = -1; for (const p of D.pages) { if (p.kind === 'cover') cur++; if (p.n === n) return cur; } return cur; };

function render() {
  $('#sp-file').textContent = `${D.file} · ${D.pages.length} pagina's, één keer door de documentinvoer`;
  $('#sp-strip').innerHTML = D.pages.map(p => {
    const d = pageDoc[p.n];
    return `<button class="sp-pg d${laneOf(p.n)}" data-n="${p.n}" data-kind="${p.kind}"${d ? ` data-doc="${esc(d.id)}"` : ''} style="${cell(p.n)}" aria-label="Pagina ${p.n}">
      <span class="im"></span><span class="tg">${p.kind === 'blank' ? 'leeg' : p.kind === 'cover' ? 'QR ' + esc(p.ref) : ''}</span>${p.n}</button>`;
  }).join('');
  $('#sp-lanes').innerHTML = lanes.map(l => `<section class="sp-lane d${l.i}" data-lane="${esc(l.nr)}">
      <div class="sp-lane-h"><span class="av">${esc(l.nr)}</span><span><b>${esc(l.client.name)}</b><span class="sub">${esc([l.client.legal_form, l.client.activity].filter(Boolean).join(' · '))}</span></span>
        <span class="lights" hidden></span></div>
      <div class="sp-docs">${l.docs.map(d => `<a class="sp-doc" href="/werkbank#doc=${encodeURIComponent(d.id)}" data-doc="${esc(d.id)}" title="${esc(d.pages.length > 1 ? d.pages.length + " pagina's bij elkaar: " + (d.split_reason || '') : 'Open in de werkbank')}">
          <span class="ths">${d.pages.slice(0, 3).map(n => `<i class="sp-th" style="${cell(n)}"></i>`).join('')}</span>
          <span><b>${esc(d.result.counterparty || D.kinds[d.kind] || 'Document')}</b><small>${esc(D.kinds[d.kind] || d.kind)}${d.pages.length > 1 ? ' · ' + d.pages.length + ' pag.' : ''}${d.result.gross_total != null ? ' · € ' + num(d.result.gross_total) : ''}</small></span>
          <span class="l-${d.light}"><i class="lamp"></i>${d.status === 'approved' ? '<i class="st ok">✓</i>' : d.status === 'asked' ? '<i class="st ask">?</i>' : d.status === 'rejected' ? '<i class="st out">×</i>' : ''}</span></a>`).join('')}</div></section>`).join('');
}

function setStat() {
  $('#st-pages').textContent = D.pages.length;
  $('#st-blank').textContent = stat.blank; $('#st-cover').textContent = stat.cover; $('#st-docs').textContent = stat.docs;
}
function phase(n) { $$('#sp-phases button').forEach((b, i) => { b.classList.toggle('done', i + 1 < n); b.classList.toggle('now', i + 1 === n); }); }
function clear() {
  timers.forEach(clearTimeout); timers = [];
  stat.blank = stat.cover = stat.docs = 0; setStat(); phase(0);
  $$('.sp-pg').forEach(p => p.classList.remove('is-blank', 'is-cover', 'sorted', 'join', 'scan'));
  $$('.sp-lane, .sp-doc').forEach(x => x.classList.remove('in', 'lit'));
  $$('.sp-lane .lights').forEach(x => { x.hidden = true; });
  $('#sp-sum').classList.remove('in');
}

/* the four steps, each as a list of small moves; play() spreads them over time, show() applies them at once */
function moves() {
  const m = [];
  const blanks = D.pages.filter(p => p.kind === 'blank'), covers = D.pages.filter(p => p.kind === 'cover');
  m.push({t: 250, p: 0, fn: () => phase(1)});
  blanks.forEach((p, i) => m.push({t: 400 + i * 110, p: 1, fn: () => { $(`.sp-pg[data-n="${p.n}"]`).classList.add('is-blank'); stat.blank++; setStat(); }}));
  const t2 = 400 + blanks.length * 110 + 350;
  m.push({t: t2, p: 1, fn: () => phase(2)});
  covers.forEach((p, i) => m.push({t: t2 + 150 + i * 320, p: 2, fn: () => {
    const el = $(`.sp-pg[data-n="${p.n}"]`); el.classList.add('scan', 'is-cover'); stat.cover++; setStat();
    const lane = $(`.sp-lane[data-lane="${p.ref}"]`); if (lane) lane.classList.add('in');
  }}));
  const t3 = t2 + 150 + covers.length * 320 + 350;
  m.push({t: t3, p: 2, fn: () => phase(3)});
  docs.forEach((d, i) => m.push({t: t3 + 150 + i * 85, p: 3, fn: () => {
    d.pages.forEach((n, k) => { const el = $(`.sp-pg[data-n="${n}"]`); if (el) { el.classList.add('sorted'); if (k < d.pages.length - 1) el.classList.add('join'); } });
    const card = $(`.sp-doc[data-doc="${CSS.escape(d.id)}"]`); if (card) card.classList.add('in');
    stat.docs++; setStat();
  }}));
  const t4 = t3 + 150 + docs.length * 85 + 400;
  m.push({t: t4, p: 3, fn: () => phase(4)});
  docs.forEach((d, i) => m.push({t: t4 + 100 + i * 35, p: 4, fn: () => { const card = $(`.sp-doc[data-doc="${CSS.escape(d.id)}"]`); if (card) card.classList.add('lit'); }}));
  m.push({t: t4 + 100 + docs.length * 35 + 250, p: 4, fn: () => {
    phase(5);
    lanes.forEach(l => {
      const c = {green: 0, amber: 0, red: 0}; l.docs.forEach(d => c[d.light]++);
      const el = $(`.sp-lane[data-lane="${l.nr}"] .lights`);
      el.innerHTML = `<i class="l-green"></i>${c.green}<i class="l-amber"></i>${c.amber}<i class="l-red"></i>${c.red}`; el.hidden = false;
    });
    const c = {green: 0, amber: 0, red: 0}; docs.forEach(d => c[d.light]++);
    const seen = docs.filter(d => d.status !== 'review').length;
    $('#sp-sum p').innerHTML = `<b>${docs.length} documenten</b> staan klaar voor controle: ${c.green} in orde, ${c.amber} met een aandachtspunt, ${c.red} met een rode vlag. Niemand heeft een pagina gesorteerd, hernoemd of in een map gezet.${seen ? ` U heeft er al ${seen} beoordeeld.` : ''}`;
    $('#sp-sum').classList.add('in');
  }});
  return m;
}
function play() { clear(); if (calm) return show(); moves().forEach(x => timers.push(setTimeout(x.fn, x.t))); }
function show() { clear(); moves().forEach(x => x.fn()); }

function tip(html) { $('#sp-tip').innerHTML = html; }
root.addEventListener('click', e => {
  const pg = e.target.closest('.sp-pg');
  if (pg) {
    const n = +pg.dataset.n, p = D.pages.find(x => x.n === n), d = pageDoc[n];
    if (d) { location.href = '/werkbank#doc=' + encodeURIComponent(d.id); return; }
    if (p.kind === 'blank') tip(`Pagina ${n} is een lege achterkant. Die wordt overgeslagen, niet weggegooid: de ruwe scan blijft bewaard tot de export is bevestigd.`);
    else if (p.kind === 'cover') { const c = D.clients[p.ref] || {}; tip(`Pagina ${n} is het scheidingsblad van dossier <b>${esc(p.ref)} · ${esc(c.name || '')}</b>. Alles tot het volgende blad hoort bij dit dossier. De QR-code wordt gelezen met vaste code, niet met AI.`); }
    return;
  }
  if (e.target.closest('#sp-play')) return play();
  if (e.target.closest('#sp-skip')) return show();
  const ph = e.target.closest('#sp-phases button');
  if (ph) { const upto = +ph.dataset.p; clear(); moves().filter(x => x.p <= upto).forEach(x => x.fn()); }   // the pile as it is after this step
});
root.addEventListener('mouseover', e => {
  const card = e.target.closest('.sp-doc'), pg = e.target.closest('.sp-pg');
  $$('.hl', root).forEach(x => x.classList.remove('hl'));
  const id = card ? card.dataset.doc : pg ? pg.dataset.doc : null;
  if (!id) return;
  $$(`[data-doc="${CSS.escape(id)}"]`, root).forEach(x => x.classList.add('hl'));
  const d = docs.find(x => x.id === id);
  if (d && pg) tip(`Pagina ${pg.dataset.n}: <b>${esc(d.result.counterparty || '')}</b>, ${esc((D.kinds[d.kind] || d.kind).toLowerCase())}${d.pages.length > 1 ? `. ${d.pages.length} pagina's bij elkaar: ${esc(d.split_reason || '')}` : ''}. Klik om het document in de werkbank te openen.`);
});

/* scheidingsbladen: the firm's own name on the sheet, preview from the same code that makes the PDF */
const firm = $('#sh-firm'), prev = $('#sh-prev'), go = $('#sh-go');
let prevT = null;
function sheetUrl(ext, one) {
  const q = new URLSearchParams();
  const nrs = one ? [one] : $$('#sh-clients input:checked').map(i => i.value);
  nrs.forEach(n => q.append('nr', n));
  if (firm.value.trim()) q.set('kantoor', firm.value.trim());
  return `/klanten/${ext}?${q}`;
}
function sheets() {
  const picked = $$('#sh-clients input:checked').map(i => i.value);
  go.classList.toggle('hide', !picked.length); $('#sh-none').classList.toggle('hide', !!picked.length);
  go.href = sheetUrl('scheidingsbladen.pdf');
  go.firstChild.textContent = picked.length === 1 ? 'Genereer het kantoor-scheidingsblad (PDF) ' : `Genereer ${picked.length} kantoor-scheidingsbladen (PDF) `;
  clearTimeout(prevT);
  prevT = setTimeout(() => {
    if (!picked.length) return;
    const img = new Image(); prev.classList.add('loading');
    img.onload = () => { prev.replaceChildren(img); prev.classList.remove('loading'); };
    img.onerror = () => prev.classList.remove('loading');
    img.alt = 'Voorbeeld van het scheidingsblad'; img.src = sheetUrl('scheidingsblad.jpg', picked[0]);
  }, 350);
}

async function start() {
  try { D = await (await fetch('/api/voorbeeld.json', {credentials: 'same-origin'})).json(); }
  catch (e) { $('#sp-strip').innerHTML = '<p class="sub">De voorbeeldstapel kon niet laden. Ververs de pagina.</p>'; return; }
  try { O = JSON.parse(localStorage.getItem('scanstraat.tour.' + D.sha) || '{}') || {}; } catch (e) { O = {}; }
  buildDocs(); render();
  $('#sh-clients').innerHTML = lanes.map(l => `<label><input type="checkbox" value="${esc(l.nr)}" checked> ${esc(l.nr)} · ${esc(l.client.name)}</label>`).join('');
  $('#sh-clients').addEventListener('change', sheets); firm.addEventListener('input', sheets); sheets();
  let seen = false; try { seen = sessionStorage.getItem('scanstraat.split') === '1'; sessionStorage.setItem('scanstraat.split', '1'); } catch (e) { /* private mode */ }
  if (seen || location.hash === '#bladen') show(); else timers.push(setTimeout(play, 450));
}
start();
})();
