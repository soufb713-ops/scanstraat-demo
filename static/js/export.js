/* Exporteren: look at what leaves before it leaves.
   Staff: the preview of their own approved documents (/api/export/preview); downloads go through the forms on the page.
   Tour: the approvals live in the visitor's browser; they are sent once to a stateless endpoint that builds the files
   with the same code as a real export. Every file from the tour is marked CONCEPT. */
(() => {
'use strict';
const hub = document.getElementById('hub');
if (!hub) return;
const SIM = hub.dataset.sim === '1';
const CSRF = document.querySelector('meta[name=csrf]').content;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const nowIso = () => { const d = new Date(), p = n => String(n).padStart(2, '0'); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };
const fmt = v => typeof v === 'number' ? v.toLocaleString('nl-NL', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : (v ?? '');

let D = null, O = null, B = null, tab = 'ubl', pick = 0;
const KEY = () => 'scanstraat.tour.' + D.sha;
const save = () => { try { localStorage.setItem(KEY(), JSON.stringify(O)); } catch (e) { /* private mode */ } };

function pile() { // the visitor's pile as the werkbank shows it
  const list = [];
  for (const d of D.docs) {
    const st = (D.staged || []).find(s => s.id === d.id);
    if (st && O.split[d.id]) st.parts.forEach(k => list.push(D.alternates.find(a => a.id === k)));
    else if (O.parts[d.id]) O.parts[d.id].forEach(k => list.push(O.derived[k]));
    else list.push(d);
  }
  for (const [line, b] of Object.entries(O.bank || {})) if (b.reply && D.replies[line]) list.push(D.replies[line].doc);
  return list.filter(Boolean);
}
const statusOf = d => ((O.docs || {})[d.id] || {}).status || 'review';
function items() {
  const all = pile(), active = all.filter(d => statusOf(d) !== 'rejected' && d.batch === 'voorbeeld').map(d => d.id);
  return all.filter(d => statusOf(d) === 'approved').map(d => {
    const o = O.docs[d.id] || {};
    const it = {id: d.id, active, status: 'approved', at: (o.approved || {}).at, notes: Object.fromEntries(Object.entries(o.notes || {}).map(([k, v]) => [k, v.note]))};
    if (o.edit) Object.assign(it, o.edit);
    if (O.derived && O.derived[d.id]) it.pages = O.derived[d.id].pages;
    return it;
  });
}
async function post(want, extra) {
  const r = await fetch('/api/voorbeeld/export', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-CSRF': CSRF},
    body: JSON.stringify(Object.assign({docs: items(), want}, extra || {}))});
  if (!r.ok) throw new Error(r.status === 400 ? 'Nog niets goedgekeurd in de rondleiding.' : 'Het exportbestand kon niet worden gemaakt (' + r.status + ').');
  return r;
}

async function load() {
  try {
    if (SIM) {
      D = await (await fetch('/api/voorbeeld.json', {credentials: 'same-origin'})).json();
      O = {v: 3, docs: {}, split: {}, parts: {}, derived: {}, bank: {}, undo: [], run: null, manual: 4, hints: {}};
      try { const o = JSON.parse(localStorage.getItem(KEY()) || 'null'); if (o && o.v === O.v) Object.assign(O, o); } catch (e) { /* private mode */ }
      B = await (await post('preview')).json();
    } else {
      B = await (await fetch('/api/export/preview', {credentials: 'same-origin'})).json();
    }
  } catch (e) { B = {count: 0, ubl: [], skipped: [], register: {cols: [], rows: []}, lines: {cols: [], rows: []}, tree: [], exact: ''}; }
  bar(); if (!$('#insp').hidden || !B.count) { open(tab, false); }
}

function bar() {
  const rules = B.ubl.reduce((n, u) => n + u.checks.length, 0), bad = B.ubl.reduce((n, u) => n + u.checks.filter(c => !c.ok).length, 0);
  $('#n-docs').textContent = B.count; $('#n-ubl').textContent = B.ubl.length; $('#n-lines').textContent = B.lines.rows.length;
  $('#n-rules').textContent = rules ? (bad ? `${rules - bad}/${rules}` : `${rules} ✓`) : '0';
  $('#t-ubl').textContent = B.ubl.length; $('#t-reg').textContent = B.register.rows.length; $('#t-tree').textContent = B.tree.length;
  $('#hub-go').disabled = false;
  $('#hub-go').textContent = B.count ? `Exporteer batch (${B.count})` : 'Exporteer batch';
}

/* ------------------------------------------------------------------ inspector */
function hl(xml) { // a light XML colouring: tags, attributes, values, comments
  return esc(xml).split('\n').map(line => '<span class="l">' + line
    .replace(/(&lt;!--.*?--&gt;)/g, '<span class="c">$1</span>')
    .replace(/(&lt;\/?)([\w:.-]+)/g, '$1<span class="t">$2</span>')
    .replace(/([\w:.-]+)=(&quot;.*?&quot;)/g, '<span class="a">$1</span>=<span class="v">$2</span>') + '</span>').join('');
}
function empty() {
  const greens = SIM ? pile().filter(d => statusOf(d) === 'review' && d.light === 'green').length : 0;
  return `<div class="insp-empty"><h2>Nog niets goedgekeurd</h2><p>Een export bevat alleen documenten die een medewerker heeft goedgekeurd. ${SIM ? 'Keur in de werkbank een paar documenten goed, of laat de export zien met de groene stukken.' : 'Keur eerst documenten goed in de werkbank.'}</p>
    <a class="btn" href="/werkbank">Naar de werkbank</a>${greens ? ` <button class="btn primary" data-act="greens" type="button">Keur de ${greens} groene documenten goed en bekijk de export</button>` : ''}</div>`;
}
function foot(note, buttons) { return `<div class="insp-foot"><span class="sub">${note}</span>${buttons}</div>`; }
const dl = (want, label, cls = '') => SIM ? `<button class="btn sm ${cls}" data-want="${want}" type="button">${label}</button>` : '';

function viewUbl() {
  if (!B.ubl.length) return `<div class="insp-empty"><h2>Geen inkoopstukken in deze batch</h2><p>SI-UBL wordt gemaakt van goedgekeurde inkoopfacturen, bonnen en creditnota's.</p></div>`;
  pick = Math.min(pick, B.ubl.length - 1);
  const u = B.ubl[pick], ok = u.checks.every(c => c.ok);
  return `<div class="ubl-view"><div class="ubl-list">${B.ubl.map((x, i) => `<button class="${i === pick ? 'on' : ''}" data-ubl="${i}" type="button"><b>${esc(x.title)}</b><i class="${x.checks.every(c => c.ok) ? '' : 'bad'}">${x.checks.filter(c => c.ok).length}/${x.checks.length}</i><small>${esc(x.client)} · ${x.kb} kB</small></button>`).join('')}</div>
    <div class="ubl-main"><div class="ubl-head"><span class="nm">${esc(u.name)}</span>${SIM ? '<span class="badge amber plain">CONCEPT · voorbeeldstapel</span>' : ''}<span class="badge ${ok ? 'green' : 'red'} plain">${ok ? 'alle regels kloppen' : 'regel klopt niet'}</span>
        ${SIM ? `<button class="btn sm" data-want="ubl" data-id="${esc(u.id)}" type="button">Download dit bestand</button>` : `<a class="btn sm" href="/document/${esc(u.id)}/ubl.xml">Download dit bestand</a>`}</div>
      <ul class="rules">${u.checks.map(c => `<li class="${c.ok ? '' : 'bad'}"><i>${c.ok ? '✓' : '✕'}</i><code>${esc(c.rule)}</code><span>${esc(c.text)}<small>${esc(c.detail)}</small></span></li>`).join('')}</ul>
      <pre class="code" tabindex="0" aria-label="SI-UBL 2.0 van ${esc(u.title)}">${hl(u.xml)}</pre></div></div>` +
    foot(`${B.ubl.length} bestand(en), één per inkoopdocument, met de pdf als bijlage in het bestand (hier ingekort weergegeven).${B.skipped.length ? ' ' + B.skipped.length + ' verkoopfactuur/-facturen gaan mee in het register en de boekingsregels, niet als inkoop-UBL.' : ''}`,
      SIM ? dl('ubl.zip', 'Download alle SI-UBL (ZIP)', 'primary') : '<button class="btn sm primary" data-submit="ubl" type="button">UBL-bestanden (ZIP)</button>');
}
function table(t) {
  return `<table><tr>${t.cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr>${t.rows.map(r => `<tr>${r.map(v => `<td${typeof v === 'number' ? ' class="num"' : ''} title="${esc(v ?? '')}">${esc(fmt(v))}</td>`).join('')}</tr>`).join('')}</table>`;
}
function viewRegister() {
  return `<div class="reg"><h3>Tabblad 1 · Register <small>${B.register.rows.length} documenten</small></h3>${table(B.register)}<h3>Tabblad 2 · Boekingsregels <small>${B.lines.rows.length} regels</small></h3>${table(B.lines)}</div>` +
    foot('Zo komt het in Excel: elke regel een document, met wie controleerde en wanneer. De kolommen zijn aan te passen aan het register dat het kantoor nu bijhoudt.',
      SIM ? dl('register.xlsx', 'Download het register (.xlsx)', 'primary') : '<a class="btn sm primary" href="/afhandelen/register.xlsx?all=1">Download het register (.xlsx)</a>');
}
function viewTree() {
  const by = {};
  B.tree.forEach(t => { ((by[t.dossier] = by[t.dossier] || {})[t.folder] = by[t.dossier][t.folder] || []).push(t.file); });
  return `<div class="tree">${Object.entries(by).map(([dossier, folders]) => `<div><h4><span class="av">${esc(dossier.split(' ')[0])}</span>${esc(dossier.split(' ').slice(1).join(' ').replace(/^[-–·]\s*/, '') || dossier)}</h4><ul>${Object.entries(folders).sort().map(([f, files]) => `<li class="dir">${esc(f)}/</li>${files.sort().map(x => `<li class="file">${esc(x)}</li>`).join('')}`).join('')}</ul></div>`).join('')}</div>` +
    foot('Elke pdf krijgt een vaste naam: datum, afzender, soort en kenmerk. De mapnamen stelt het kantoor één keer in.',
      SIM ? dl('mijnkantoor.zip', 'Download de dossiermappen (ZIP)', 'primary') : '<button class="btn sm primary" data-submit="mijnkantoor" type="button">ZIP voor upload</button>');
}
function viewExact() {
  if (!B.exact) return `<div class="insp-empty"><h2>Geen boekingen in deze batch</h2><p>Brieven, polissen en pakbonnen gaan naar het dossier, niet naar de boekhouding.</p></div>`;
  return `<pre class="code" tabindex="0" aria-label="Importbestand voor Exact Online">${hl(B.exact)}</pre>` +
    foot('Importbestand met de inkoop- en verkoopboekingen: dagboek, grootboekrekening en btw-code per regel. Met de koppeling gaat dezelfde boeking rechtstreeks, met de pdf als bijlage.',
      SIM ? dl('exact.xml', 'Download XML', 'primary') + dl('exact.csv', 'Download CSV') : '<button class="btn sm primary" data-submit="xml" type="button">Importbestand (XML)</button><button class="btn sm" data-submit="csv" type="button">CSV</button>');
}
function open(which, scroll = true) {
  tab = which || tab;
  const insp = $('#insp'); insp.hidden = false;
  $$('.insp-tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  $('#insp-body').innerHTML = !B.count ? empty() : tab === 'ubl' ? viewUbl() : tab === 'register' ? viewRegister() : tab === 'tree' ? viewTree() : viewExact();
  if (scroll) insp.scrollIntoView({behavior: 'smooth', block: 'start'});
  if (SIM && B.count && !O.hints.exported) { O.hints.exported = 1; save(); }
}

async function download(want, id) {
  try {
    const r = await post(want, id ? {id} : null);
    const name = (/filename\*?=(?:UTF-8'')?"?([^";]+)/i.exec(r.headers.get('Content-Disposition') || '') || [])[1] || want;
    const a = document.createElement('a'); a.href = URL.createObjectURL(await r.blob()); a.download = decodeURIComponent(name);
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  } catch (e) { alertBar(e.message); }
}
function alertBar(text) {
  let el = $('#hub-alert');
  if (!el) { el = document.createElement('div'); el.id = 'hub-alert'; el.className = 'flash error'; $('#hub-bar').before(el); }
  el.textContent = text; setTimeout(() => el.remove(), 6000);
}

hub.addEventListener('click', e => {
  const t = e.target;
  if (t.closest('#hub-go')) return open('ubl');
  const tb = t.closest('.insp-tabs [data-tab]'); if (tb) return open(tb.dataset.tab, false);
  const op = t.closest('[data-open]'); if (op) return open(op.dataset.open);
  const u = t.closest('[data-ubl]'); if (u) { pick = +u.dataset.ubl; const top = $('.ubl-list').scrollTop; open('ubl', false); $('.ubl-list').scrollTop = top; return; }
  const w = t.closest('[data-want]'); if (w) { if (!B.count) return open(tab); return download(w.dataset.want, w.dataset.id); }
  const sub = t.closest('[data-submit]'); if (sub) { const f = $(`form[data-form="${sub.dataset.submit}"]`); if (f) f.requestSubmit ? f.requestSubmit() : f.submit(); return; }
  if (t.closest('[data-act=greens]')) {
    pile().filter(d => statusOf(d) === 'review' && d.light === 'green').forEach(d => {
      const o = O.docs[d.id] || (O.docs[d.id] = {});
      o.status = 'approved'; o.approved = {by: D.user, at: nowIso()};
      (o.trail = o.trail || []).push({ts: nowIso(), event: 'approved', label: 'Goedgekeurd', actor: D.user, text: 'Geen open punten (in één keer vanaf de exportpagina)'});
    });
    save(); load().then(() => open('ubl'));
  }
});
load();
})();
