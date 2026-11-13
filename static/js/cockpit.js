/* Werkbank: the keyboard-first review cockpit.
   Two stores behind one screen:
   - live: staff work on their own documents through /api/document/<id> (same rules as the forms);
   - sim:  a tour guest works on the fictional sample. Everything the guest does is kept in the guest's own browser
           (localStorage, keyed by the sample's hash, so it survives a server restart). Checks after an edit come
           from the real check code through a stateless request. Nothing is stored on the server. */
(() => {
'use strict';
const root = document.getElementById('cockpit');
if (!root) return;
const SIM = root.dataset.sim === '1';
const CSRF = document.querySelector('meta[name=csrf]').content;
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const clone = o => JSON.parse(JSON.stringify(o));
const eur = v => (v === null || v === undefined || v === '') ? '–' : '€ ' + Number(v).toLocaleString('nl-NL', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const num = v => (v === null || v === undefined || v === '') ? '' : Number(v).toLocaleString('nl-NL', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const parseNum = t => { t = String(t || '').replace(/[€\s]/g, ''); if (!t) return null; if (t.includes(',')) t = t.replace(/\./g, '').replace(',', '.'); const n = Number(t); return Number.isFinite(n) ? Math.round(n * 100) / 100 : null; };
const MONTHS = ['jan', 'feb', 'mrt', 'apr', 'mei', 'jun', 'jul', 'aug', 'sep', 'okt', 'nov', 'dec'];
const dShort = iso => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || ''); return m ? `${+m[3]} ${MONTHS[+m[2] - 1]}` : ''; };
const dNl = iso => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || ''); return m ? `${m[3]}-${m[2]}-${m[1]}` : (iso || ''); };
const tShort = iso => { const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso || ''); return m ? `${+m[3]} ${MONTHS[+m[2] - 1]} ${m[4]}:${m[5]}` : ''; };
const nowIso = () => { const d = new Date(), p = n => String(n).padStart(2, '0'); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; };
const TOUCH = matchMedia('(pointer:coarse)').matches;

const FIELDS = [ // key, label, css colour variable
  ['counterparty', 'Leverancier', 'party'], ['reference', 'Factuurnummer', 'ref'], ['doc_date', 'Factuurdatum', 'date'],
  ['due_date', 'Vervaldatum', 'due'], ['iban', 'IBAN', 'iban'], ['supplier_kvk', 'KvK-nummer', 'id'],
  ['supplier_vat_number', 'Btw-nummer', 'id'], ['net_total', 'Excl. btw', 'net'], ['vat_21', 'Btw 21%', 'v21'],
  ['vat_9', 'Btw 9%', 'v9'], ['vat_0', 'Btw 0% / verlegd', 'v0'], ['gross_total', 'Totaal', 'gross']];
const FLABEL = Object.fromEntries(FIELDS.map(f => [f[0], f[1]]));
const FCOL = Object.fromEntries(FIELDS.map(f => [f[0], f[2]]));
const BOOKABLE = ['inkoopfactuur', 'kassabon', 'creditnota', 'verkoopfactuur'];
const LIGHT = {green: 'In orde', amber: 'Let op', red: 'Rode vlag'};

let D = null;            // payload from the server
let docs = [], bank = [];
let O = {v: 3, docs: {}, split: {}, parts: {}, derived: {}, bank: {}, undo: [], run: null, manual: 4, hints: {}}; // sim overlay
const S = {sel: null, kind: 'doc', page: null, field: null, tab: 'todo', order: 'wave', client: '', zoom: 1, view: 'doc',
           armed: null, modal: null, busy: false, liveUndo: []};

/* ------------------------------------------------------------------ store */
const KEY = () => 'scanstraat.tour.' + D.sha;
function load() { try { const o = JSON.parse(localStorage.getItem(KEY()) || 'null'); if (o && o.v === O.v) O = Object.assign(O, o); } catch (e) { /* private mode */ } }
function save() { try { localStorage.setItem(KEY(), JSON.stringify(O)); } catch (e) { /* private mode: the tour still works */ } }
function ov(id) { return O.docs[id] || (O.docs[id] = {}); }

function merge(base) {
  const o = O.docs[base.id];
  const d = clone((o && o.server) || base);
  d.status = (o && o.status) || 'review';
  d.approved = o && o.approved || null; d.asked = o && o.asked || null; d.rejected = o && o.rejected || null;
  const notes = (o && o.notes) || {};
  d.flags = d.flags.map(f => Object.assign({}, f, {note: notes[f.id] || null}));
  d.handled = d.flags.filter(f => f.level === 'error').every(f => f.note);
  d.trail = (base.trail || []).concat((o && o.trail) || []);
  d.corrections = (o && o.corrections) || [];
  return d;
}

function build() {
  if (!SIM) { docs = D.docs; bank = D.bank; return; }
  const list = [];
  for (const d of D.docs) {
    const st = D.staged.find(s => s.id === d.id);
    if (st && O.split[d.id]) st.parts.forEach(k => list.push(D.alternates.find(a => a.id === k)));
    else if (O.parts[d.id]) O.parts[d.id].forEach(k => list.push(O.derived[k]));
    else list.push(d);
  }
  for (const [line, b] of Object.entries(O.bank)) if (b.reply && D.replies[line]) list.push(D.replies[line].doc);
  docs = list.filter(Boolean).map(merge);
  bank = D.bank.map(l => {
    const b = O.bank[l.id];
    if (!b) return l;
    const out = Object.assign({}, l, {chase: b});
    if (b.closed) out.status = 'afgehandeld';
    else if (b.reply) { out.status = 'ontvangen'; out.doc = D.replies[l.id].doc.id; }
    else out.status = 'gevraagd';
    return out;
  });
}
const byId = id => docs.find(d => d.id === id);
const lineById = id => bank.find(l => l.id === id);
const client = nr => D.clients[nr] || null;
const chartOf = d => { const c = client(d.client_nr); return (c && D.charts[c.chart]) || D.charts.standaard || []; };
const activeIds = () => docs.filter(d => d.status !== 'rejected' && d.batch === 'voorbeeld').map(d => d.id);

async function post(url, data, json) {
  const opt = {method: 'POST', headers: {'X-CSRF': CSRF}, credentials: 'same-origin'};
  if (json) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(data); }
  else { const f = new URLSearchParams(); for (const [k, v] of Object.entries(data)) (Array.isArray(v) ? v : [v]).forEach(x => f.append(k, x ?? '')); opt.body = f; }
  const r = await fetch(url, opt);
  if (!r.ok) throw new Error(r.status === 403 ? 'Dit kan niet in de rondleiding.' : r.status === 400 ? 'Sessie verlopen: ververs de pagina.' : 'Er ging iets mis (' + r.status + ').');
  return r.json();
}

function trail(id, event, label, text) {
  const o = ov(id);
  (o.trail = o.trail || []).push({ts: nowIso(), event, label, actor: D.user, text: text || ''});
}
function snapshot(what) { O.undo.push({what, docs: clone(O.docs), split: clone(O.split), parts: clone(O.parts), bank: clone(O.bank)}); if (O.undo.length > 30) O.undo.shift(); }

/* ------------------------------------------------------------------ actions (both stores) */
async function simCheck(id) {
  const o = ov(id), base = docs.find(d => d.id === id) || {};
  const body = {id, active: activeIds(), notes: Object.fromEntries(Object.entries(o.notes || {}).map(([k, v]) => [k, v.note]))};
  if (o.edit) Object.assign(body, o.edit);
  if (O.derived[id]) body.pages = O.derived[id].pages;
  const fresh = await post('/api/voorbeeld/check', body, true);
  if (O.derived[id]) O.derived[id] = Object.assign({}, fresh, {flags: fresh.flags.map(f => Object.assign({}, f, {note: null}))});
  o.server = fresh;
  if (base.trail) o.server.trail = [];
  return fresh;
}

async function approve(id, force) {
  const d = byId(id);
  if (!d || d.status !== 'review') return;
  if (!d.client_nr) return toast('Kies eerst het dossier van dit document.', 'bad');
  if (d.light === 'red' && !d.handled) {
    shake(); return toast('Rode vlag open: leg met <kbd>R</kbd> vast wat is gecontroleerd, of vraag de klant met <kbd>W</kbd>.', 'bad', true);
  }
  if (d.light !== 'green' && !force && S.armed !== id) {
    S.armed = id; renderPanelActions();
    return toast(d.light === 'red' ? 'Rode vlag met notitie. Nogmaals <kbd>Enter</kbd> om goed te keuren.' : 'Er staat een aandachtspunt open. Nogmaals <kbd>Enter</kbd> om toch goed te keuren.', 'warn', true);
  }
  S.armed = null;
  const open = d.flags.filter(f => f.level !== 'info').length;
  tick();
  if (SIM) {
    snapshot('approve');
    Object.assign(ov(id), {status: 'approved', approved: {by: D.user, at: nowIso()}});
    trail(id, 'approved', 'Goedgekeurd', open ? `${open} punt(en) gezien` + (d.flags.some(f => f.note) ? '. Notities: ' + d.flags.filter(f => f.note).map(f => f.note.note).join(' | ') : '') : 'Geen open punten');
    save(); build(); advance(id);
  } else {
    d.status = 'approved'; S.liveUndo.push({type: 'doc', id}); advance(id);
    try { const r = await post('/api/document/' + id, {action: 'approve'}); apply(r); if (!r.ok) { toast(esc(r.message), 'bad'); select(id); } }
    catch (e) { toast(esc(e.message), 'bad'); await reload(); }
  }
}

async function reject(id, note) {
  const d = byId(id);
  if (!d || !['review', 'asked'].includes(d.status)) return;
  note = note || (d.flags.some(f => /ubbel/.test(f.message)) ? 'Dubbel exemplaar' : d.kind === 'overig' ? 'Niet voor de administratie' : 'Uit de stapel gehaald');
  if (SIM) {
    snapshot('reject');
    Object.assign(ov(id), {status: 'rejected', rejected: {by: D.user, at: nowIso(), note}});
    trail(id, 'rejected', 'Uit stapel gehaald', note);
    save(); build(); advance(id);
    const same = docs.filter(x => x.id !== id && x.client_nr === d.client_nr && x.status === 'review' && norm(x.result.counterparty) === norm(d.result.counterparty));
    for (const x of same) { try { await simCheck(x.id); } catch (e) { /* offline: the old flags stay */ } }
    if (same.length) { save(); build(); renderAll(); }
  } else {
    d.status = 'rejected'; S.liveUndo.push({type: 'doc', id}); advance(id);
    try { apply(await post('/api/document/' + id, {action: 'reject', note})); } catch (e) { toast(esc(e.message), 'bad'); await reload(); }
  }
  toast('Uit de stapel gehaald. <kbd>U</kbd> zet het terug.');
}
const norm = t => String(t || '').toLowerCase().replace(/[^a-z0-9]/g, '');

async function reopen(id) {
  if (SIM) { snapshot('reopen'); const o = ov(id); o.status = 'review'; delete o.approved; delete o.rejected; delete o.asked; trail(id, 'reopened', 'Heropend', ''); save(); build(); select(id); }
  else { try { const r = await post('/api/document/' + id, {action: 'reopen'}); if (!r.ok) toast(esc(r.message), 'bad'); apply(r); select(id); } catch (e) { toast(esc(e.message), 'bad'); } }
}

async function addNote(id, flagId, text) {
  const d = byId(id), f = d && d.flags.find(x => x.id === flagId);
  if (!f) return;
  if (SIM) {
    snapshot('note');
    const o = ov(id); (o.notes = o.notes || {})[flagId] = {note: text, by: D.user, at: nowIso(), flag: f.message};
    trail(id, 'flag_noted', 'Notitie bij ' + (f.level === 'error' ? 'rode vlag' : 'aandachtspunt'), `${f.message} Notitie: ${text}`);
    save(); build(); renderAll();
  } else {
    try { const r = await post('/api/document/' + id, {action: 'flagnote', flag: flagId, note: text}); if (!r.ok) toast(esc(r.message), 'bad'); else S.liveUndo.push({type: 'note', id, flag: flagId}); apply(r); renderAll(); }
    catch (e) { toast(esc(e.message), 'bad'); }
  }
  toast('Notitie vastgelegd in het controlespoor.', 'ok');
}
async function removeNote(id, flagId) {
  if (SIM) { snapshot('unnote'); const o = ov(id); if (o.notes) delete o.notes[flagId]; trail(id, 'flag_note_removed', 'Notitie verwijderd', ''); save(); build(); renderAll(); }
  else { try { apply(await post('/api/document/' + id, {action: 'unnote', flag: flagId})); renderAll(); } catch (e) { toast(esc(e.message), 'bad'); } }
}

async function saveEdit(id, edit, changed) {
  const d = byId(id);
  if (!d) return;
  if (SIM) {
    const o = ov(id);
    o.edit = Object.assign(o.edit || {}, edit, {result: Object.assign((o.edit && o.edit.result) || {}, edit.result || {})});
    (o.corrections = o.corrections || []).push({field: changed, by: D.user, at: nowIso()});
    trail(id, 'corrected', 'Gecorrigeerd', 'Aangepast: ' + changed);
    try { await simCheck(id); } catch (e) { toast(esc(e.message), 'bad'); }
    save(); build(); renderAfterEdit(id);
  } else {
    const r0 = d.result, form = {action: 'save'};
    const r = Object.assign({}, r0, edit.result || {});
    if ('client_nr' in edit) form.client_nr = edit.client_nr || '';
    if (edit.kind) form.kind = edit.kind;
    ['counterparty', 'reference', 'doc_date', 'due_date', 'supplier_vat_number', 'iban', 'description'].forEach(k => { form[k] = r[k] ?? ''; });
    ['net_total', 'gross_total'].forEach(k => { form[k] = r[k] === null || r[k] === undefined ? '' : String(r[k]).replace('.', ','); });
    const lines = r.lines || [];
    form.line_account = lines.map(l => l.account); form.line_vat = lines.map(l => l.vat_code);
    form.line_excl = lines.map(l => String(l.amount_excl ?? '').replace('.', ',')); form.line_vatamt = lines.map(l => String(l.vat_amount ?? 0).replace('.', ','));
    form.line_desc = lines.map(l => l.description || '');
    if (!lines.length) { form.line_account = ['']; form.line_vat = ['']; form.line_excl = ['']; form.line_vatamt = ['']; form.line_desc = ['']; }
    if (edit.filing) { form.folder = edit.filing.folder; form.filename = edit.filing.filename; }
    try { apply(await post('/api/document/' + id, form)); renderAfterEdit(id); } catch (e) { toast(esc(e.message), 'bad'); }
  }
}

async function split(id, after) {
  const d = byId(id);
  if (!d || d.pages.length < 2 || d.status !== 'review') return toast('Dit document heeft één pagina: er valt niets te splitsen.', 'warn');
  after = after || S.page || d.pages[0];
  if (after === d.pages[d.pages.length - 1]) after = d.pages[d.pages.length - 2];
  if (SIM) {
    snapshot('split');
    const staged = D.staged.find(s => s.id === id);
    if (staged) { O.split[id] = true; save(); build(); select(staged.parts[0]); toast('Gesplitst: elke bon heeft nu een eigen voorstel.', 'ok'); return; }
    const base = id.split('#')[0], k = d.pages.indexOf(after) + 1;
    const a = base + '#' + d.pages[0], b = base + '#' + d.pages[k];
    O.derived[a] = Object.assign(clone(d), {id: a, pages: d.pages.slice(0, k)});
    O.derived[b] = Object.assign(clone(d), {id: b, pages: d.pages.slice(k)});
    const parent = D.docs.find(x => x.id === base) ? base : Object.keys(O.parts).find(p => O.parts[p].includes(id));
    O.parts[parent] = (O.parts[parent] || [id]).flatMap(x => x === id ? [a, b] : [x]);
    build();
    try { await simCheck(a); await simCheck(b); } catch (e) { toast(esc(e.message), 'bad'); }
    save(); build(); select(a); toast('Gesplitst na pagina ' + after + '. <kbd>U</kbd> maakt het ongedaan.', 'ok');
  } else {
    try { const r = await post(`/api/scan/${d.batch}/indeling`, {doc: id, action: 'split', after}); if (!r.ok) return toast(esc(r.message), 'bad'); toast('Gesplitst. De delen worden opnieuw gelezen.', 'ok'); await reload(true); }
    catch (e) { toast(esc(e.message), 'bad'); }
  }
}
async function mergePrev(id) {
  const d = byId(id);
  if (!d) return;
  if (SIM) {
    const st = D.staged.find(s => s.parts.includes(id));
    const parent = Object.keys(O.parts).find(p => O.parts[p].includes(id));
    if (!st && !parent) return toast('Samenvoegen kan in de rondleiding alleen met een document dat u zelf heeft gesplitst.', 'warn');
    snapshot('merge');
    if (st) { delete O.split[st.id]; st.parts.forEach(k => delete O.docs[k]); save(); build(); select(st.id); }
    else { O.parts[parent].forEach(k => { delete O.derived[k]; delete O.docs[k]; }); delete O.parts[parent]; save(); build(); select(parent); }
    toast('Samengevoegd.', 'ok');
  } else {
    try { const r = await post(`/api/scan/${d.batch}/indeling`, {doc: id, action: 'merge'}); if (!r.ok) return toast(esc(r.message), 'bad'); toast('Samengevoegd met het vorige document.', 'ok'); await reload(true); }
    catch (e) { toast(esc(e.message), 'bad'); }
  }
}

async function undo() {
  if (SIM) {
    const u = O.undo.pop();
    if (!u) return toast('Niets om ongedaan te maken.');
    O.docs = u.docs; O.split = u.split; O.parts = u.parts; O.bank = u.bank; save(); build();
    if (!byId(S.sel) && !lineById(S.sel)) S.sel = null;
    renderAll(); toast('Ongedaan gemaakt.');
  } else {
    const u = S.liveUndo.pop();
    if (!u) return toast('Niets om ongedaan te maken.');
    if (u.type === 'note') await removeNote(u.id, u.flag); else await reopen(u.id);
    toast('Ongedaan gemaakt.');
  }
}

/* message to the client: about a document (ask) or about a bank line without a document (chase) */
function question(d) {
  const c = client(d.client_nr) || {}, first = (c.contact || '').split(' ')[0] || 'daar';
  const what = `${(D.kinds[d.kind] || 'het document').toLowerCase()} van ${d.result.counterparty || 'onbekend'}` + (d.result.doc_date ? ` (${dNl(d.result.doc_date)})` : '');
  const msgs = d.flags.filter(f => f.level !== 'info').map(f => f.message).join(' ');
  let ask = 'Kun je een leesbare kopie of een korte toelichting sturen?';
  if (/akelijk doel|horeca/i.test(msgs)) ask = 'Met wie was dit en wat was het zakelijke doel? Dan boeken we hem goed.';
  else if (/priv[eé]/i.test(msgs)) ask = 'Is deze aankoop zakelijk of privé?';
  else if (/pakbon/i.test(msgs)) ask = 'We missen de factuur die bij deze pakbon hoort. Kun je die sturen?';
  else if (/IBAN/.test(msgs)) ask = 'Het rekeningnummer op deze factuur is anders dan op de eerdere facturen. Betaal hem nog niet: wij controleren het nummer eerst bij de leverancier.';
  else if (/Btw klopt niet|Rekenfout/i.test(msgs)) ask = 'Het btw-bedrag op deze factuur klopt niet. Kun je bij de leverancier een correctiefactuur opvragen?';
  else if (/activeren/i.test(msgs)) ask = 'Is dit een nieuwe machine of een reparatie van een bestaande? Dat bepaalt hoe we hem boeken.';
  return {wa: `Hoi ${first}, we hebben een vraag over de ${what}. ${ask} Groet, ${D.firm}`,
          mail: `Beste ${c.contact || 'klant'},\n\nBij het verwerken van uw administratie hebben we een vraag over de ${what}.\n\n${ask.replace(/Kun je/g, 'Kunt u').replace(/ je /g, ' u ')}\n\nAlvast bedankt.\n\nMet vriendelijke groet,\n${D.firm}`};
}

async function sendAsk(target, m) { // target: {doc} or {line}; m: {channel, body, send, read, wwft}
  const sent = m.send && m.read && m.wwft;
  if (m.send && !sent) return toast('Vink beide verklaringen aan voordat het bericht weg mag.', 'bad');
  if (target.line) {
    const l = lineById(target.line), c = client(l.client_nr) || {};
    if (c.wwft_stop && m.send) return toast('Voor deze klant geldt een Wwft-stop: het bericht kan niet worden verstuurd.', 'bad');
    if (SIM) {
      snapshot('chase');
      O.bank[l.id] = {by: D.user, at: nowIso(), channel: m.channel, body: m.body, sent: sent ? nowIso() : null};
      save(); build(); closeModal(); select(l.id, 'bank'); S.view = 'client'; renderAll();
      toast(sent ? 'Goedgekeurd en verstuurd (gesimuleerd): kijk mee op de telefoon van de klant.' : 'Concept klaargezet. Er is nog niets verstuurd.', 'ok');
      if (sent && D.replies[l.id]) playReply(l.id);
    } else {
      try {
        const r = await post('/api/bank/' + l.id, {action: m.send ? 'send' : 'ask', channel: m.channel, body: m.body, confirm_read: m.read ? 1 : '', confirm_wwft: m.wwft ? 1 : ''});
        D.bank = r.bank; build(); closeModal(); select(l.id, 'bank'); S.view = 'client'; renderAll(); toast(esc(r.message), 'ok');
      } catch (e) { toast(esc(e.message), 'bad'); }
    }
    return;
  }
  const d = byId(target.doc);
  if (SIM) {
    snapshot('ask');
    Object.assign(ov(d.id), {status: 'asked', asked: {by: D.user, at: nowIso(), channel: m.channel, body: m.body, sent: sent ? nowIso() : null}});
    trail(d.id, 'asked_client', 'Vraag aan klant', (m.channel === 'whatsapp' ? 'WhatsApp' : 'E-mail') + (sent ? ': goedgekeurd en verstuurd (gesimuleerd)' : ': concept, nog niet verstuurd'));
    save(); build(); closeModal(); advance(d.id);
    toast(sent ? 'Goedgekeurd en verstuurd (gesimuleerd). Het document wacht op de klant.' : 'Concept klaargezet. Er is nog niets verstuurd.', 'ok');
  } else {
    try {
      const r = await post('/api/document/' + d.id, {action: m.send ? 'send' : 'ask', channel: m.channel, body: m.body, note: m.body.slice(0, 200), confirm_read: m.read ? 1 : '', confirm_wwft: m.wwft ? 1 : ''});
      if (!r.ok) return toast(esc(r.message), 'bad');
      S.liveUndo.push({type: 'doc', id: d.id}); apply(r); closeModal(); advance(d.id); toast(esc(r.message), 'ok');
    } catch (e) { toast(esc(e.message), 'bad'); }
  }
}

let replyTimers = [];
function playReply(lineId) { // sim: the client's phone answers with a photo, which is read and matched to the bank line
  replyTimers.forEach(clearTimeout); replyTimers = [];
  const step = (ms, fn) => replyTimers.push(setTimeout(fn, ms));
  const set = (k, v) => { if (O.bank[lineId]) { O.bank[lineId][k] = v; save(); build(); renderAll(); } };
  step(1400, () => set('read', nowIso()));
  step(2600, () => set('typing', true));
  step(5200, () => {
    if (!O.bank[lineId]) return;
    Object.assign(O.bank[lineId], {typing: false, reply: nowIso()});
    const rd = D.replies[lineId].doc;
    trail(rd.id, 'matched', 'Gekoppeld aan bankregel', `${lineById(lineId).type_label} ${dNl(lineById(lineId).date)} · ${eur(Math.abs(lineById(lineId).amount))}`);
    save(); build(); renderAll();
    toast(`Foto ontvangen van ${esc((client(lineById(lineId).client_nr) || {}).contact || 'de klant')}: gelezen en gekoppeld aan de pinbetaling. <a href="#" data-go="${esc(rd.id)}">Bekijk de bon</a>`, 'ok', true);
  });
}
async function demoReply(lineId) {
  try { const r = await post('/api/bank/' + lineId, {action: 'demo_reply'}); toast(esc(r.message), r.ok ? 'ok' : 'bad'); await reload(true); } catch (e) { toast(esc(e.message), 'bad'); }
}
async function closeLine(lineId, note) {
  if (SIM) { snapshot('close'); O.bank[lineId] = {closed: true, by: D.user, at: nowIso(), note}; save(); build(); renderAll(); }
  else { try { const r = await post('/api/bank/' + lineId, {action: 'close', note}); D.bank = r.bank; build(); renderAll(); } catch (e) { toast(esc(e.message), 'bad'); } }
  toast('Vastgelegd: geen bon nodig.', 'ok');
}
async function reopenLine(lineId) {
  if (SIM) { snapshot('reopenline'); delete O.bank[lineId]; save(); build(); renderAll(); }
  else { try { const r = await post('/api/bank/' + lineId, {action: 'reopen'}); D.bank = r.bank; build(); renderAll(); } catch (e) { toast(esc(e.message), 'bad'); } }
}

function apply(r) { // live: fold a server answer into the page's data
  if (!r) return;
  const put = d => { const i = D.docs.findIndex(x => x.id === d.id); if (i >= 0) D.docs[i] = Object.assign({}, d, {trail: d.trail && d.trail.length ? d.trail : D.docs[i].trail}); };
  if (r.doc) put(r.doc);
  (r.docs || []).forEach(put);
  if (r.bank) D.bank = r.bank;
  build();
}
async function reload(wait) {
  const q = new URLSearchParams(); if (root.dataset.scan) q.set('scan', root.dataset.scan); if (S.sel && S.kind === 'doc') q.set('doc', S.sel);
  for (let i = 0; i < 40; i++) {
    const r = await fetch('/api/cockpit?' + q, {credentials: 'same-origin'}); D = await r.json(); build();
    if (!wait || !D.reading) break;
    await new Promise(res => setTimeout(res, 900));
  }
  if (!byId(S.sel) && !lineById(S.sel)) S.sel = null;
  renderAll();
}

/* ------------------------------------------------------------------ queue */
const rank = {green: 0, amber: 1, red: 2};
function todo() {
  let list = docs.filter(d => d.status === 'review' && (!S.client || d.client_nr === S.client));
  if (S.order === 'wave') list = list.slice().sort((a, b) => rank[a.light] - rank[b.light]);
  else if (S.order === 'risk') list = list.slice().sort((a, b) => rank[b.light] - rank[a.light]);
  return list;
}
const doneList = () => docs.filter(d => d.status !== 'review' && (!S.client || d.client_nr === S.client));
const missing = () => bank.filter(l => (l.status !== 'gekoppeld' && l.status !== 'open') && (!S.client || l.client_nr === S.client));

function select(id, kind) {
  S.sel = id; S.kind = kind || (lineById(id) && !byId(id) ? 'bank' : 'doc'); S.field = null; S.zoom = 1; S.armed = null;
  const d = S.kind === 'doc' ? byId(id) : null;
  S.page = d ? d.pages[0] : null;
  if (S.kind === 'doc') S.view = 'doc';
  if (d && d.status !== 'review') S.tab = 'done'; else if (d) S.tab = 'todo';
  if (history.replaceState) history.replaceState(null, '', '#' + (S.kind === 'bank' ? 'bon=' : 'doc=') + encodeURIComponent(id));
  renderAll();
}
function advance(from) {
  const list = todo();
  if (!list.length) { S.sel = null; renderAll(); return finish(); }
  const order = S.order === 'pile' ? docs.map(d => d.id) : null;
  let next = list[0];
  if (order) { const i = order.indexOf(from); next = list.find(d => order.indexOf(d.id) > i) || list[0]; }
  select(next.id);
}
function move(step) {
  const items = S.tab === 'todo' ? todo().map(d => ['doc', d.id]).concat(missing().map(l => ['bank', l.id])) : doneList().map(d => ['doc', d.id]);
  if (!items.length) return;
  let i = items.findIndex(x => x[1] === S.sel);
  i = i < 0 ? 0 : Math.max(0, Math.min(items.length - 1, i + step));
  select(items[i][1], items[i][0]);
  const el = $('.q-item.on'); if (el) el.scrollIntoView({block: 'nearest'});
}

function thumb(d) {
  if (d.image) return `<span class="q-th" style="background-image:url('${esc(d.image)}')"></span>`;
  if (SIM) { const n = d.pages[0] - 1; return `<span class="q-th" style="background-image:url('${D.sprite}');background-size:${14 * 30}px ${3 * 42}px;background-position:-${(n % 14) * 30}px -${Math.floor(n / 14) * 42}px"></span>`; }
  return `<span class="q-th" style="background-image:url('/pagina/${esc(d.batch)}/${d.pages[0]}.jpg?t=1')"></span>`;
}
function qItem(d) {
  const st = d.status === 'approved' ? '<i class="st ok" title="Goedgekeurd">✓</i>' : d.status === 'asked' ? '<i class="st ask" title="Vraag aan klant">?</i>' : d.status === 'rejected' ? '<i class="st out" title="Uit de stapel">×</i>' : '';
  const sub = [D.kinds[d.kind] || d.kind, d.client_nr || 'dossier?', d.pages.length > 1 ? d.pages.length + ' pag.' : ''].filter(Boolean).join(' · ');
  return `<button class="q-item ${d.id === S.sel ? 'on' : ''} l-${d.light}${d.light === 'red' && d.handled ? ' handled' : ''}" data-doc="${esc(d.id)}">
    ${thumb(d)}<span class="q-main"><b>${esc(d.result.counterparty || D.kinds[d.kind] || 'Document')}</b><small>${esc(sub)}</small></span>
    <span class="q-amt">${d.result.gross_total === null || d.result.gross_total === undefined ? '' : num(d.result.gross_total)}${st}</span></button>`;
}
function qLine(l) {
  const c = client(l.client_nr) || {};
  const chip = {ontbreekt: ['red', 'geen bon'], gevraagd: ['blue', 'gevraagd'], ontvangen: ['green', 'ontvangen'], afgehandeld: ['grey', 'afgehandeld']}[l.status] || ['grey', l.status];
  return `<button class="q-item q-bank ${l.id === S.sel ? 'on' : ''}" data-line="${esc(l.id)}"><span class="q-th bank">${esc(l.type_label || '')}</span>
    <span class="q-main"><b>${esc(l.shop)}</b><small>${dShort(l.date)} · ${esc(c.name || l.client_nr)}</small></span>
    <span class="q-amt">${num(Math.abs(l.amount))}<i class="badge plain ${chip[0]}">${chip[1]}</i></span></button>`;
}
function renderQueue() {
  const t = todo(), dn = doneList(), ms = missing();
  const counts = {green: 0, amber: 0, red: 0}; t.forEach(d => counts[d.light]++);
  let h = `<div class="q-tabs"><button class="${S.tab === 'todo' ? 'on' : ''}" data-tab="todo">Te doen <b>${t.length}</b></button><button class="${S.tab === 'done' ? 'on' : ''}" data-tab="done">Klaar <b>${dn.length}</b></button></div>`;
  if (S.tab === 'todo') {
    h += `<div class="q-meta"><span class="lights"><i class="l-green"></i>${counts.green}<i class="l-amber"></i>${counts.amber}<i class="l-red"></i>${counts.red}</span>
      <select id="q-order" title="Volgorde (G)"><option value="wave"${S.order === 'wave' ? ' selected' : ''}>Groene golf eerst</option><option value="risk"${S.order === 'risk' ? ' selected' : ''}>Risico eerst</option><option value="pile"${S.order === 'pile' ? ' selected' : ''}>Volgorde van de stapel</option></select></div>`;
    h += `<div class="q-list">${t.map(qItem).join('') || '<p class="q-empty">Alles is beoordeeld.</p>'}`;
    h += `<div class="q-sect" id="bonnen"><span>Ontbrekende bonnen</span><b>${ms.filter(l => l.status === 'ontbreekt').length}</b></div>${ms.map(qLine).join('') || '<p class="q-empty">Elke bankregel heeft een document.</p>'}</div>`;
  } else {
    h += `<div class="q-list">${dn.map(qItem).join('') || '<p class="q-empty">Nog niets afgehandeld.</p>'}</div>`;
  }
  if (SIM) h += missions();
  $('.ck-queue').innerHTML = h;
}
function missions() {
  const appr = docs.filter(d => d.status === 'approved').length;
  const m = [
    [appr >= 10, `Keur 10 stukken goed met <kbd>Enter</kbd> <small>${Math.min(appr, 10)}/10</small>`],
    [!!O.split['M-TANK+M-BOUWMARKT'], 'Splits de twee bonnen op één document met <kbd>S</kbd>', 'M-TANK+M-BOUWMARKT'],
    [docs.some(d => d.id === 'M-BGR-IBAN' && (d.handled || d.status !== 'review')), 'Handel de IBAN-vlag af met <kbd>R</kbd>', 'M-BGR-IBAN'],
    [Object.values(O.bank).some(b => b.sent), 'Vraag een ontbrekende bon op met <kbd>W</kbd>', 'B1057-05'],
    [!!O.hints.exported, '<a href="/afhandelen">Exporteer de batch</a> en bekijk de SI-UBL'],
  ];
  return `<div class="q-missions"><h3>Vijf dingen om te proberen</h3>${m.map(x => `<div class="${x[0] ? 'ok' : ''}"${x[2] ? ` data-jump="${x[2]}"` : ''}><i>${x[0] ? '✓' : ''}</i><span>${x[1]}</span></div>`).join('')}
    <button class="btn sm ghost" data-act="reset">Rondleiding opnieuw beginnen</button></div>`;
}

/* ------------------------------------------------------------------ canvas */
function imgUrl(d, n) { return d.image ? d.image : D.img.replace('{batch}', d.batch).replace('{n}', n); }
function renderCanvas() {
  const el = $('.ck-canvas');
  const d = S.kind === 'doc' ? byId(S.sel) : null, l = S.kind === 'bank' ? lineById(S.sel) : null;
  if (!d && !l) { el.innerHTML = emptyCanvas(); return; }
  const c = client((d || l).client_nr) || {};
  const tabs = `<div class="cv-tabs"><button class="${S.view === 'doc' ? 'on' : ''}" data-view="doc">${d ? 'Document' : 'Bankregel'}</button><button class="${S.view === 'client' ? 'on' : ''}" data-view="client">Klant-ervaring <small>MKB / ZZP zicht</small></button></div>`;
  if (S.view === 'client') { el.innerHTML = `<div class="cv-bar">${tabs}</div>` + clientView(d, l, c); return; }
  if (l) { el.innerHTML = `<div class="cv-bar">${tabs}</div>` + bankCanvas(l, c); return; }
  if (d.purged) { el.innerHTML = `<div class="cv-bar">${tabs}</div><div class="cv-empty"><h2>Origineel gewist</h2><p>Na de bevestigde export zijn het document, de paginabeelden en de koppeltabel verwijderd. De gecontroleerde velden en het controlespoor blijven bewaard.</p></div>`; return; }
  const pi = d.pages.indexOf(S.page) < 0 ? 0 : d.pages.indexOf(S.page);
  S.page = d.pages[pi];
  const boxes = Object.entries(d.boxes || {}).filter(([, b]) => b.page === S.page || d.image);
  const pages = d.pages.length > 1 ? `<div class="cv-pages">${d.pages.map((p, i) => `<button class="${p === S.page ? 'on' : ''}" data-page="${p}">${i + 1}</button>${i < d.pages.length - 1 && d.status === 'review' ? `<button class="cut" data-split="${p}" title="Splits na deze pagina (S)">✂</button>` : ''}`).join('')}<span class="sub">pagina ${pi + 1} van ${d.pages.length} · scanpagina ${S.page}</span></div>` : `<span class="cv-one sub">${d.image ? 'foto via WhatsApp' : 'scanpagina ' + S.page}</span>`;
  el.innerHTML = `<div class="cv-bar">${tabs}${pages}<span class="cv-zoom"><button data-zoom="out" title="Uitzoomen">−</button><button data-zoom="fit" title="Passend (Z)">${Math.round(S.zoom * 100)}%</button><button data-zoom="in" title="Inzoomen">+</button>${!SIM && !d.image ? `<a class="btn sm ghost" href="/document/${esc(d.id)}.pdf" target="_blank" rel="noopener">PDF</a>` : ''}</span></div>
    <div class="cv-stage" tabindex="-1"><div class="cv-sheet${d.image ? ' photo' : ''}">
      <img src="${esc(imgUrl(d, S.page))}" alt="Pagina ${S.page} van ${esc(d.result.counterparty || 'document')}" draggable="false">
      ${boxes.map(([k, b]) => `<button class="box f-${FCOL[k] || 'party'}${k === S.field ? ' on' : ''}" data-box="${k}" style="left:${b.box[0] * 100}%;top:${b.box[1] * 100}%;width:${(b.box[2] - b.box[0]) * 100}%;height:${(b.box[3] - b.box[1]) * 100}%"><span>${esc(k === 'counterparty' && d.kind === 'verkoopfactuur' ? 'Afnemer' : FLABEL[k] || k)}</span></button>`).join('')}
    </div></div>`;
  const img = $('.cv-sheet img', el);
  if (img && !img.complete) img.addEventListener('load', () => { fit(); if (S.field) focusBox(S.field, true); }, {once: true});
  fit();
  if (S.field) focusBox(S.field, true);
}
function fit() { // 100% shows the whole page when that stays readable, else the page at reading width; zoom multiplies from there
  const stage = $('.cv-stage'), sheet = $('.cv-sheet');
  if (!stage || !sheet) return;
  const img = $('img', sheet);
  const ar = img && img.naturalWidth ? img.naturalWidth / img.naturalHeight : 0.7071;
  const cs = getComputedStyle(stage);
  const w = stage.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
  const h = stage.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
  const narrow = matchMedia('(max-width:860px)').matches;
  const base = narrow ? w : Math.max(Math.min(w, 560 * Math.max(ar / 0.7071, 0.6)), Math.min(w, h * ar));
  sheet.style.width = Math.round(Math.max(200, base) * S.zoom) + 'px';
}
function emptyCanvas() {
  const left = todo().length;
  return `<div class="cv-empty"><h2>${left ? 'Kies een document' : 'De stapel is leeg'}</h2><p>${left ? 'Gebruik <kbd>J</kbd> en <kbd>K</kbd> om door de stapel te lopen.' : 'Alles is beoordeeld. Bekijk wat klaar staat voor uw pakket.'}</p>${left ? '' : '<a class="btn primary" href="/afhandelen">Naar exporteren</a>'}</div>`;
}
function focusBox(key, instant) {
  const stage = $('.cv-stage'), sheet = $('.cv-sheet'), d = byId(S.sel);
  if (!stage || !sheet || !d) return;
  $$('.box.on', sheet).forEach(b => b.classList.remove('on'));
  const b = d.boxes && d.boxes[key];
  if (!b) { toastOnce('nobox', 'Voor dit veld is geen positie op het document bekend.'); return; }
  if (b.page !== S.page && !d.image) { S.page = b.page; S.field = key; return renderCanvas(); }
  const bw = b.box[2] - b.box[0];
  const want = Math.max(1.6, Math.min(3, 0.34 / Math.max(bw, 0.04)));
  if (S.zoom < want) { S.zoom = want; fit(); const z = $('[data-zoom=fit]'); if (z) z.textContent = Math.round(S.zoom * 100) + '%'; }
  const el = $(`.box[data-box="${key}"]`, sheet);
  if (el) { el.classList.add('on'); el.classList.remove('pulse'); void el.offsetWidth; el.classList.add('pulse'); }
  requestAnimationFrame(() => {
    const cx = (b.box[0] + b.box[2]) / 2 * sheet.offsetWidth, cy = (b.box[1] + b.box[3]) / 2 * sheet.offsetHeight;
    stage.scrollTo({left: cx - stage.clientWidth / 2, top: cy - stage.clientHeight / 2, behavior: instant ? 'auto' : 'smooth'});
  });
}
function zoom(how) {
  const steps = [1, 1.5, 2, 2.5, 3];
  if (how === 'fit') S.zoom = S.zoom > 1 ? 1 : 2;
  else { const i = steps.findIndex(z => z >= S.zoom - 0.01); S.zoom = steps[Math.max(0, Math.min(steps.length - 1, (i < 0 ? 0 : i) + (how === 'in' ? 1 : -1)))]; }
  if (S.zoom === 1) S.field = null;
  renderCanvas();
}

function phone(c, bubbles, footer) {
  return `<div class="phone"><div class="ph-top"><span class="ph-av">${esc((D.firm || 'K').slice(0, 1))}</span><span><b>${esc(D.firm)}</b><small>Zakelijk account · het eigen nummer van het kantoor</small></span></div>
    <div class="ph-chat">${bubbles || '<div class="ph-day">Nog geen bericht</div>'}</div>
    <div class="ph-in"><span>${footer || 'Bericht'}</span><i>▶</i></div></div>`;
}
function bubble(text, me, meta) { return `<div class="bub ${me ? 'me' : ''}"><p>${esc(text).replace(/\n/g, '<br>')}</p><small>${meta || ''}</small></div>`; }
function chatFor(d, l) { // what the client sees for this document or bank line
  let h = '<div class="ph-day">Vandaag</div>';
  if (l) {
    const b = O.bank[l.id] || (l.chase && !l.chase.closed ? l.chase : null);
    if (!b || b.closed) return h + `<div class="ph-ghost">${esc(l.text)}</div><div class="ph-note">Voorbeeld van het bericht. Het gaat pas weg nadat een medewerker het heeft goedgekeurd.</div>`;
    h += bubble(b.body || l.text, false, (b.sent || !SIM ? (b.read || b.reply ? 'gelezen' : 'afgeleverd') : 'concept, nog niet verstuurd'));
    if (b.typing) h += '<div class="bub me typing"><i></i><i></i><i></i></div>';
    if (b.reply && D.replies[l.id]) h += `<div class="bub me img"><img src="${esc(D.replies[l.id].image)}" alt="Foto van de bon"><small>foto · zojuist</small></div>` + bubble('Top, we hebben hem. Hij staat klaar bij de rest van je administratie.', false, 'automatische ontvangstbevestiging');
    return h;
  }
  if (d && d.asked) return h + bubble(d.asked.body || d.asked.note || '', false, d.asked.sent || !SIM ? 'afgeleverd' : 'concept, nog niet verstuurd');
  if (d) return h + `<div class="ph-ghost">${esc(question(d).wa)}</div><div class="ph-note">Voorbeeld. Druk op <kbd>W</kbd> om de vraag klaar te zetten.</div>`;
  return h;
}
function clientView(d, l, c) {
  const first = esc((c.contact || 'de klant').split(' ')[0]);
  return `<div class="cv-client"><div>${phone(c, chatFor(d, l), 'Foto of bericht')}</div>
    <div class="cv-side"><h2>Zo ziet ${first} het</h2>
      <p>De klant krijgt één korte vraag in WhatsApp, in de naam van het kantoor. Een foto terugsturen is genoeg: die komt in de werkbank, wordt gelezen en aan de bankregel gekoppeld.</p>
      <ul class="plain"><li><b>Geen app, geen inlog</b> voor de klant.</li><li><b>Nooit automatisch:</b> elk bericht is een concept tot een medewerker het goedkeurt (Wwft).</li><li><b>Eigen nummer:</b> het WhatsApp Business-nummer van het kantoor zelf.</li></ul>
      <div class="honest">Zonder WhatsApp-koppeling werkt dezelfde vraag per e-mail, of met de persoonlijke aanleverlink van de klant.</div>
      ${c.upload ? `<a class="btn" href="${esc(c.upload)}" target="_blank" rel="noopener">Open de aanleverpagina van ${first}</a>` : ''}
      ${l && !SIM && D.demo && D.replies[l.id] && l.status === 'gevraagd' ? `<button class="btn" data-act="demo-reply">Speel het antwoord van de klant af (demo)</button>` : ''}
    </div></div>`;
}
function bankCanvas(l, c) {
  const st = {ontbreekt: 'Geen document bij deze betaling', gevraagd: 'Bon opgevraagd bij de klant', ontvangen: 'Bon ontvangen en gekoppeld', afgehandeld: 'Afgehandeld zonder bon'}[l.status] || l.status;
  return `<div class="cv-bank"><div class="bank-card s-${l.status}">
      <div class="bk-top"><span class="mono">${esc(l.type_label || 'Bank')}</span><span>${dNl(l.date)}</span></div>
      <div class="bk-amt">${eur(Math.abs(l.amount))}</div>
      <div class="bk-desc">${esc(l.description)}</div>
      <div class="bk-st"><i></i>${esc(st)}${l.doc ? ` · <a href="#" data-go="${esc(l.doc)}">open het document</a>` : ''}</div>
      ${l.chase && l.chase.closed ? `<div class="sub">${esc(l.chase.note || '')} · ${esc(l.chase.by || '')}</div>` : ''}
    </div>
    <div class="bank-how"><h3>Bonnenjacht</h3><p>Deze bankregel van <b>${esc(c.name || '')}</b> heeft geen document in de administratie. Normaal ontdekt u dat bij de btw-aangifte. Hier staat de vraag aan de klant al klaar.</p>
      <div class="bank-msg">${esc(l.text)}</div>
      <div class="row">${l.status === 'ontbreekt' ? `<button class="btn primary" data-act="ask">Vraag ${esc((c.contact || 'de klant').split(' ')[0])} via WhatsApp <kbd>W</kbd></button><button class="btn" data-act="close-line">Geen bon nodig</button>` : l.status === 'afgehandeld' || l.status === 'gevraagd' ? `<button class="btn" data-act="reopen-line">${l.status === 'gevraagd' ? 'Vraag intrekken' : 'Heropenen'}</button>` : ''}
      <button class="btn ghost" data-view="client">Bekijk het klantzicht</button></div></div></div>`;
}

/* ------------------------------------------------------------------ panel */
function renderPanel() {
  const el = $('.ck-panel');
  const d = S.kind === 'doc' ? byId(S.sel) : null, l = S.kind === 'bank' ? lineById(S.sel) : null;
  if (l) { el.innerHTML = bankPanel(l); return; }
  if (!d) { el.innerHTML = summaryPanel(); return; }
  const r = d.result, c = client(d.client_nr), lock = d.status !== 'review' || d.purged;
  const bookable = BOOKABLE.includes(d.kind), sales = d.kind === 'verkoopfactuur';
  const dis = lock ? ' disabled' : '';
  const sw = k => `<i class="sw f-${FCOL[k]}${d.boxes && d.boxes[k] ? '' : ' none'}"></i>`;
  const fld = (k, label, cls = '', type = 'text') => `<label class="fld ${cls}" data-f="${k}">${sw(k)}<span>${label}</span><input data-k="${k}" type="${type}" value="${esc(r[k] ?? '')}"${dis} autocomplete="off" spellcheck="false"></label>`;
  const amt = (k, label, v) => `<label class="fld amt" data-f="${k}">${sw(k)}<span>${label}</span><input data-k="${k}" inputmode="decimal" value="${num(v)}"${dis} autocomplete="off"></label>`;
  let h = `<div class="pn-head l-${d.light}"><div class="pn-light"><span class="lamp"></span><b>${d.status === 'approved' ? 'Goedgekeurd' : d.status === 'asked' ? 'Vraag aan klant' : d.status === 'rejected' ? 'Uit de stapel' : LIGHT[d.light]}</b>
      ${d.status !== 'review' ? `<small>${esc((d.approved || d.asked || d.rejected || {}).by || '')} · ${tShort((d.approved || d.asked || d.rejected || {}).at)}</small>` : d.light === 'red' && d.handled ? '<small>vlag afgehandeld met notitie</small>' : ''}</div>
      <div class="pn-id mono">${esc(d.id)}</div></div>
    <div class="pn-row2"><label><span>Soort</span><select data-edit="kind"${dis}>${Object.entries(D.kinds).map(([k, v]) => `<option value="${k}"${k === d.kind ? ' selected' : ''}>${esc(v)}</option>`).join('')}</select></label>
      <label><span>Dossier</span><select data-edit="client_nr"${dis}><option value="">Kies het dossier</option>${Object.values(D.clients).map(x => `<option value="${esc(x.nr)}"${x.nr === d.client_nr ? ' selected' : ''}>${esc(x.nr)} · ${esc(x.name)}</option>`).join('')}</select></label></div>
    <p class="pn-via">${d.client_via ? 'Dossier via ' + esc(d.client_via) : 'Dossier nog niet bekend'}${d.pages.length > 1 ? ` · ${d.pages.length} pagina's bij elkaar: ${esc(d.split_reason || '')}` : ''}</p>`;
  h += `<section><h3>Gelezen velden <small>klik een veld: het document zoomt mee</small></h3><div class="flds">
      ${fld('counterparty', sales ? 'Afnemer' : bookable ? 'Leverancier' : 'Afzender', 'wide')}
      ${fld('reference', bookable ? 'Factuurnummer' : 'Kenmerk')}${fld('doc_date', bookable ? 'Factuurdatum' : 'Datum', '', 'date')}
      ${bookable ? fld('iban', 'IBAN', 'mono') : ''}${fld('due_date', bookable ? 'Vervaldatum' : 'Termijn', '', 'date')}
      ${bookable ? fld('supplier_kvk', 'KvK-nummer', 'mono') + fld('supplier_vat_number', 'Btw-nummer', 'mono') : ''}
    </div>`;
  if (bookable) {
    const vl = r.vat_lines || [];
    h += `<div class="flds amts">${amt('net_total', 'Excl. btw', r.net_total)}
      ${vl.map((v, i) => `<label class="fld amt" data-f="vat_${Number(v.rate) === 21 ? 21 : Number(v.rate) === 9 ? 9 : 0}">${sw('vat_' + (Number(v.rate) === 21 ? 21 : Number(v.rate) === 9 ? 9 : 0))}<span>Btw ${num(v.rate).replace(',00', '')}%</span><input data-vat="${i}" inputmode="decimal" value="${num(v.amount)}"${dis} autocomplete="off"></label>`).join('')}
      ${amt('gross_total', 'Totaal', r.gross_total)}</div>`;
  }
  h += '</section>';
  if (bookable) {
    const chart = chartOf(d), rgs = Object.fromEntries(chart.map(x => [x[0], x[2] || '']));
    h += `<section><h3>Boekingsvoorstel</h3><div class="lines">${(r.lines || []).map((ln, i) => `<div class="ln" data-line="${i}">
        <select data-ln="account"${dis}>${chart.some(x => x[0] === ln.account) ? '' : `<option value="${esc(ln.account)}" selected>${esc(ln.account)} (niet in schema)</option>`}${chart.map(x => `<option value="${esc(x[0])}"${x[0] === ln.account ? ' selected' : ''}>${esc(x[0])} ${esc(x[1])}</option>`).join('')}</select>
        <select data-ln="vat_code"${dis} title="Btw-code">${Object.entries(D.vat_codes).map(([k, v]) => `<option value="${k}"${k === ln.vat_code ? ' selected' : ''} title="${esc(v)}">${k}</option>`).join('')}</select>
        <input data-ln="amount_excl" inputmode="decimal" value="${num(ln.amount_excl)}"${dis} title="Bedrag excl. btw"><input data-ln="vat_amount" inputmode="decimal" value="${num(ln.vat_amount)}"${dis} title="Btw-bedrag">
        ${!lock && r.lines.length > 1 ? `<button class="x" data-rmline="${i}" title="Regel verwijderen">×</button>` : '<span></span>'}
        <small>${rgs[ln.account] ? `<span class="rgs" title="Referentiegrootboekschema (RGS)">RGS ${esc(rgs[ln.account])}</span>` : ''}<span>${esc(D.vat_codes[ln.vat_code] || '')}</span><em>${esc(ln.description || '')}</em></small></div>`).join('')}
        ${lock ? '' : '<button class="btn sm ghost" data-act="addline">+ Regel</button>'}</div>
      ${evidence(d)}${r.booking_reason ? `<p class="why"><b>Waarom:</b> ${esc(r.booking_reason)}</p>` : ''}</section>`;
  } else {
    h += `<section><h3>Niet boeken</h3><p class="why">${esc(D.kinds[d.kind] || 'Dit document')} gaat naar het dossier, niet naar de boekhouding.${r.description ? ' ' + esc(r.description) : ''}</p></section>`;
  }
  h += `<section id="pn-checks">${checksHtml(d)}</section>`;
  if (d.filing) h += `<section><h3>Dossiermap</h3><p class="filing mono">${esc(d.filing.folder)}/<br>${esc(d.filing.filename)}</p></section>`;
  h += `<section><h3>Controlespoor <small>per boeking</small></h3><ol class="trail">${(d.trail || []).map(t => `<li class="e-${esc(t.event)}"><b>${esc(t.label)}</b><span>${esc(t.actor)} · ${tShort(t.ts)}</span>${t.text ? `<p>${esc(t.text)}</p>` : ''}</li>`).join('')}</ol></section>`;
  h += '<div class="pn-actions"></div>';
  el.innerHTML = h;
  el.scrollTop = 0;
  renderPanelActions();
}
function evidence(d) {
  const e = d.evidence;
  if (!e) return '';
  const cls = {match: 'ok', partial: 'warn', differs: 'warn', new: 'new'}[e.status];
  return `<div class="evi ${cls}">${e.total ? `<span class="bar"><i style="width:${e.share}%"></i></span><b>${e.same}/${e.total}</b>` : '<b>nieuw</b>'}<span>${esc(e.text)}</span></div>`;
}
function checksHtml(d) {
  const flags = d.flags.filter(f => f.level !== 'info'), info = d.flags.filter(f => f.level === 'info');
  let h = '';
  if (flags.length) h += `<h3>${flags.some(f => f.level === 'error') ? 'Vlaggen' : 'Aandachtspunten'}</h3>` + flags.map(f => `<div class="flag ${f.level}${f.note ? ' noted' : ''}" data-flag="${esc(f.id)}"><p>${esc(f.message)}</p>
      ${f.note ? `<div class="note"><b>✓ ${esc(f.note.by)} · ${tShort(f.note.at)}</b> ${esc(f.note.note)}${d.status === 'review' ? ` <button data-unnote="${esc(f.id)}">verwijder</button>` : ''}</div>` : d.status === 'review' ? `<button class="btn sm" data-note="${esc(f.id)}">Notitie vastleggen <kbd>R</kbd></button>` : ''}</div>`).join('');
  if ((d.badges || []).length) h += '<h3>Controles <small>vaste regels, geen AI</small></h3><ul class="badges">' + d.badges.map(b => `<li class="${b.ok === true ? 'ok' : b.ok === false ? 'bad' : 'na'}"><i>${b.ok === true ? '✓' : b.ok === false ? '✕' : '○'}</i><span><b>${esc(b.label)}</b><small>${esc(b.detail || '')}</small></span></li>`).join('') + '</ul>';
  if (info.length) h += info.map(f => `<p class="info">${esc(f.message)}</p>`).join('');
  return h;
}
function renderPanelActions() {
  const el = $('.pn-actions'), d = S.kind === 'doc' ? byId(S.sel) : null;
  if (!el || !d) return;
  if (d.status === 'review') {
    const blocked = d.light === 'red' && !d.handled;
    el.innerHTML = `<button class="btn go lg${blocked ? ' blocked' : ''}${S.armed === d.id ? ' armed' : ''}" data-act="approve">${S.armed === d.id ? 'Nogmaals: toch goedkeuren' : 'Goedkeuren &amp; volgende'} <kbd>Enter</kbd></button>
      <button class="btn" data-act="ask">Vraag klant <kbd>W</kbd></button>${d.flags.some(f => f.level !== 'info' && !f.note) ? '<button class="btn" data-act="note">Notitie <kbd>R</kbd></button>' : ''}<button class="btn ghost" data-act="reject">Eruit <kbd>X</kbd></button>`;
  } else {
    const can = !(d.exported || []).length && !d.purged;
    el.innerHTML = `<span class="sub">${d.status === 'approved' ? 'Staat klaar voor de export.' : d.status === 'asked' ? 'Wacht op antwoord van de klant.' : 'Uit de stapel gehaald: ' + esc((d.rejected || {}).note || '')}</span>${can ? '<button class="btn" data-act="reopen">Heropenen</button>' : ''}`;
  }
}
function bankPanel(l) {
  const c = client(l.client_nr) || {};
  const same = bank.filter(x => x.client_nr === l.client_nr);
  return `<div class="pn-head l-${l.status === 'ontbreekt' ? 'red' : l.status === 'gevraagd' ? 'amber' : 'green'}"><div class="pn-light"><span class="lamp"></span><b>Bankregel ${esc(l.id)}</b></div></div>
    <section><h3>Dossier</h3><p class="why"><b>${esc(c.nr || '')} · ${esc(c.name || '')}</b><br>${esc(c.legal_form || '')}${c.activity ? ' · ' + esc(c.activity) : ''}<br>Contact: ${esc(c.contact || 'onbekend')}${c.whatsapp ? ' · WhatsApp ' + esc(c.whatsapp) : ''}</p>
      ${c.wwft_stop ? '<div class="flag error"><p>Voor deze klant geldt een Wwft-stop: er kan geen bericht worden verstuurd.</p></div>' : ''}</section>
    <section><h3>Bankregels van dit dossier <small>uit CAMT.053</small></h3><table class="mini"><tbody>${same.map(x => `<tr class="${x.id === l.id ? 'on' : ''}"><td>${dShort(x.date)}</td><td>${esc(x.shop)}</td><td class="num">${num(x.amount)}</td><td>${x.status === 'gekoppeld' || x.status === 'ontvangen' ? '<i class="ok">✓</i>' : x.status === 'ontbreekt' ? '<i class="bad">geen bon</i>' : esc(x.status)}</td></tr>`).join('')}</tbody></table></section>
    <section><h3>Zo wordt gekoppeld</h3><p class="why">Een bankregel hoort bij een document van hetzelfde dossier met exact hetzelfde bedrag. Datum en naam beslissen bij meer kandidaten. Vaste regels, geen AI.</p></section>`;
}
function summaryPanel() {
  const a = docs.filter(d => d.status === 'approved').length, q = docs.filter(d => d.status === 'asked').length, x = docs.filter(d => d.status === 'rejected').length, t = docs.filter(d => d.status === 'review').length;
  return `<section><h3>Stand van de stapel</h3><table class="mini"><tbody><tr><td>Te beoordelen</td><td class="num">${t}</td></tr><tr><td>Goedgekeurd</td><td class="num">${a}</td></tr><tr><td>Vraag aan klant</td><td class="num">${q}</td></tr><tr><td>Uit de stapel</td><td class="num">${x}</td></tr><tr><td>Bankregels zonder bon</td><td class="num">${bank.filter(l => l.status === 'ontbreekt').length}</td></tr></tbody></table></section>
    ${a ? '<section><a class="btn primary block" href="/afhandelen">Exporteer wat klaar is</a></section>' : ''}`;
}
function renderAfterEdit(id) {
  if (S.sel !== id) return renderQueue();
  const d = byId(id);
  renderQueue();
  const head = $('.pn-head'), cks = $('#pn-checks');
  if (!d || !head || !cks) return renderPanel();
  head.className = 'pn-head l-' + d.light; $('.pn-light b', head).textContent = LIGHT[d.light];
  cks.innerHTML = checksHtml(d);
  const ev = $('.evi'); if (ev) ev.outerHTML = evidence(d) || '<span class="evi hide"></span>';
  const fl = $('.filing'); if (fl && d.filing) fl.innerHTML = esc(d.filing.folder) + '/<br>' + esc(d.filing.filename);
  const via = $('.pn-via'); if (via) via.textContent = d.client_via ? 'Dossier via ' + d.client_via : 'Dossier nog niet bekend';
  renderPanelActions(); renderTop();
}

/* ------------------------------------------------------------------ top bar, speedrun timer */
let clock = null;
function tick() { // every approval counts for the speedrun
  const r = O.run || (O.run = {start: Date.now(), n: 0, marks: []});
  if (!r.start) r.start = Date.now();
  r.n++; r.marks.push(Date.now() - r.start); r.last = Date.now();
  if (r.n === 10 && !r.ten) { r.ten = r.marks[9]; setTimeout(() => toast(`<b>10 stukken in ${(r.ten / 1000).toLocaleString('nl-NL', {maximumFractionDigits: 1})} seconden.</b> Met de hand, bij ${O.manual} minuten per stuk: ${10 * O.manual} minuten.`, 'ok', true), 250); }
  if (SIM) save();
}
function renderTop() {
  const total = docs.length, left = docs.filter(d => d.status === 'review').length, r = O.run;
  const secs = r && r.start ? ((r.last || Date.now()) - r.start) / 1000 : 0;
  $('.ck-top').innerHTML = `<div class="tp-title"><b>Werkbank</b><span>${esc(D.firm)}${SIM ? ' · voorbeeldstapel' : ''}</span></div>
    <label class="tp-filter"><span>Dossier</span><select id="tp-client"><option value="">Alle dossiers</option>${Object.values(D.clients).filter(c => docs.some(d => d.client_nr === c.nr)).map(c => `<option value="${esc(c.nr)}"${c.nr === S.client ? ' selected' : ''}>${esc(c.nr)} · ${esc(c.name)}</option>`).join('')}</select></label>
    <div class="tp-run" title="Tijd sinds uw eerste goedkeuring"><span class="mono">${r && r.n ? fmtSecs(secs) : '00:00,0'}</span><small>${r && r.n ? r.n + ' goedgekeurd' : 'speedrun: 10 in 20 s'}</small></div>
    <div class="tp-prog" title="${total - left} van ${total} beoordeeld"><div><i style="width:${total ? (total - left) / total * 100 : 0}%"></i></div><small>${total - left} / ${total}</small></div>
    <button class="btn sm ghost" data-act="help" title="Sneltoetsen (?)">Sneltoetsen <kbd>?</kbd></button>`;
}
const fmtSecs = s => { const m = Math.floor(s / 60), r = s - m * 60; return String(m).padStart(2, '0') + ':' + r.toLocaleString('nl-NL', {minimumIntegerDigits: 2, minimumFractionDigits: 1, maximumFractionDigits: 1}); };
function renderKeys() {
  const d = S.kind === 'doc' ? byId(S.sel) : null;
  $('.ck-keys').innerHTML = `<span><kbd>Enter</kbd> Goedkeuren &amp; volgende</span><span><kbd>W</kbd> Vraag klant via WhatsApp</span><span class="${d && d.pages.length > 1 ? 'hot' : ''}"><kbd>S</kbd> Splits pagina</span><span><kbd>R</kbd> Rode vlag: notitie</span><span><kbd>J</kbd><kbd>K</kbd> Navigeer</span><span><kbd>X</kbd> Eruit</span><span><kbd>U</kbd> Ongedaan</span><span><kbd>Z</kbd> Zoom</span><span><kbd>?</kbd> Alles</span>`;
}
function renderAll() { renderTop(); renderQueue(); renderCanvas(); renderPanel(); renderKeys(); }

/* ------------------------------------------------------------------ modals */
function modal(html, cls) {
  closeModal();
  const m = document.createElement('div'); m.className = 'ck-modal ' + (cls || '');
  m.innerHTML = `<div class="md-box" role="dialog" aria-modal="true">${html}</div>`;
  m.addEventListener('mousedown', e => { if (e.target === m) closeModal(); });
  document.body.appendChild(m); S.modal = m;
  const f = $('[autofocus]', m) || $('textarea,input,button', m); if (f) f.focus();
  return m;
}
function closeModal() { if (S.modal) { S.modal.remove(); S.modal = null; } }

function openNote(flagId) {
  const d = byId(S.sel);
  if (!d || d.status !== 'review') return;
  const f = flagId ? d.flags.find(x => x.id === flagId) : (d.flags.find(x => x.level === 'error' && !x.note) || d.flags.find(x => x.level === 'warning' && !x.note));
  if (!f) return toast('Er staat geen open vlag op dit document.');
  const quick = /IBAN/.test(f.message) && /wijkt af/.test(f.message) ? ['Teruggebeld via het bekende nummer van de crediteur: nieuw rekeningnummer bevestigd', 'Niet bevestigd: betaling geblokkeerd, factuur wordt wel geboekt', 'Crediteur ontkent de wijziging: mogelijke factuurfraude, klant gewaarschuwd']
    : /Btw klopt niet|Rekenfout/.test(f.message) ? ['Maximaal het juiste btw-bedrag afgetrokken, correctiefactuur gevraagd', 'Afrondingsverschil van de leverancier, akkoord']
    : /ubbel/.test(f.message) ? ['Geen dubbel: deellevering met hetzelfde nummer', 'Dubbel bevestigd: één exemplaar blijft, dit gaat eruit']
    : ['Gecontroleerd met de klant: akkoord', 'Gecontroleerd tegen het origineel: akkoord', 'Besproken met de dossierverantwoordelijke'];
  const m = modal(`<h2>${f.level === 'error' ? 'Rode vlag' : 'Aandachtspunt'}: wat is gecontroleerd?</h2><p class="md-flag ${f.level}">${esc(f.message)}</p>
    <div class="md-quick">${quick.map((q, i) => `<button data-q="${i}"><kbd>${i + 1}</kbd>${esc(q)}</button>`).join('')}</div>
    <textarea rows="3" placeholder="Of schrijf zelf wat u heeft gecontroleerd" autofocus></textarea>
    <p class="sub">De notitie komt met uw naam en het tijdstip in het controlespoor van deze boeking. De vlag blijft zichtbaar.</p>
    <div class="md-foot"><button class="btn" data-close>Annuleren <kbd>Esc</kbd></button><button class="btn primary" data-ok>Vastleggen <kbd>Enter</kbd></button></div>`, 'note');
  const ta = $('textarea', m);
  const ok = () => { const t = ta.value.trim(); if (t.length < 3) return toast('Schrijf in een paar woorden wat is gecontroleerd.', 'bad'); closeModal(); addNote(d.id, f.id, t); };
  m.addEventListener('click', e => { const q = e.target.closest('[data-q]'); if (q) { ta.value = quick[+q.dataset.q]; ta.focus(); } if (e.target.closest('[data-ok]')) ok(); if (e.target.closest('[data-close]')) closeModal(); });
  m.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ok(); }
    else if (/^[1-9]$/.test(e.key) && !ta.value && quick[+e.key - 1]) { e.preventDefault(); ta.value = quick[+e.key - 1]; }
  });
}

function openAsk() {
  const d = S.kind === 'doc' ? byId(S.sel) : null, l = S.kind === 'bank' ? lineById(S.sel) : null;
  if (!d && !l) return;
  if (d && d.status !== 'review') return toast('Dit document is al afgehandeld.');
  if (l && l.status !== 'ontbreekt') return toast('Voor deze bankregel loopt al een vraag of er is al een bon.');
  const c = client((d || l).client_nr);
  if (!c) return toast('Kies eerst het dossier: dan weten we wie de vraag krijgt.', 'bad');
  const q = d ? question(d) : {wa: l.text, mail: `Beste ${c.contact || 'klant'},\n\n${l.text.replace(/^Hoi [^,]+, /, '').replace(/ Groet, .*$/, '')}\n\nMet vriendelijke groet,\n${D.firm}`};
  let channel = 'whatsapp';
  const m = modal(`<div class="md-ask"><div class="md-form"><h2>Vraag aan ${esc(c.contact || c.name)}</h2>
      <div class="seg"><button class="on" data-ch="whatsapp">WhatsApp</button><button data-ch="email">E-mail</button></div>
      <p class="sub md-to">Aan ${esc(c.whatsapp || 'het WhatsApp-nummer van de klant')} · namens ${esc(D.firm)}</p>
      <textarea rows="6" autofocus>${esc(q.wa)}</textarea>
      ${c.wwft_stop ? '<div class="flag error"><p>Voor deze klant geldt een Wwft-stop: het bericht kan wel als concept klaarstaan, maar niet worden verstuurd.</p></div>' : ''}
      <div class="approve"><label><input type="checkbox" data-c="read"> ${esc(D.confirm.read)}</label><label><input type="checkbox" data-c="wwft"> ${esc(D.confirm.wwft)}</label></div>
      <p class="sub">Niets gaat vanzelf weg. Zonder de twee verklaringen blijft het een concept onder Berichten.</p>
      <div class="md-foot"><button class="btn" data-close>Annuleren <kbd>Esc</kbd></button><button class="btn" data-draft>Klaarzetten als concept <kbd>Enter</kbd></button><button class="btn go" data-send>Goedkeuren en versturen <kbd>Ctrl</kbd><kbd>Enter</kbd></button></div></div>
    <div class="md-prev"><h3>Klant-ervaring</h3>${phone(c, '', 'Foto of bericht')}</div></div>`, 'ask');
  const ta = $('textarea', m), chat = $('.ph-chat', m);
  const draw = () => { chat.innerHTML = '<div class="ph-day">Vandaag</div>' + (channel === 'whatsapp' ? bubble(ta.value, false, 'concept') : `<div class="ph-note">E-mail: de klant krijgt dit bericht in de mailbox, niet in WhatsApp.</div>`); };
  draw(); ta.addEventListener('input', draw);
  const go = send => sendAsk(d ? {doc: d.id} : {line: l.id}, {channel, body: ta.value.trim(), send, read: $('[data-c=read]', m).checked, wwft: $('[data-c=wwft]', m).checked});
  m.addEventListener('click', e => {
    const ch = e.target.closest('[data-ch]');
    if (ch) { channel = ch.dataset.ch; $$('[data-ch]', m).forEach(b => b.classList.toggle('on', b === ch)); ta.value = channel === 'whatsapp' ? q.wa : q.mail; $('.md-to', m).textContent = (channel === 'whatsapp' ? 'Aan ' + (c.whatsapp || 'het WhatsApp-nummer van de klant') : 'Aan ' + (c.email || 'het e-mailadres van de klant')) + ' · namens ' + D.firm; draw(); }
    if (e.target.closest('[data-close]')) closeModal();
    if (e.target.closest('[data-draft]')) go(false);
    if (e.target.closest('[data-send]')) go(true);
  });
  m.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); go(true); } else if (e.key === 'Enter' && !e.shiftKey && e.target === ta) { e.preventDefault(); go(false); } });
}

function openHelp() {
  const k = [['Enter', 'Goedkeuren en naar het volgende document. Oranje vraagt een tweede Enter; rood vraagt eerst een notitie of een vraag aan de klant.'], ['W', 'Vraag aan de klant, via WhatsApp of e-mail. Altijd eerst een concept.'], ['S', 'Splits het document na de pagina die u bekijkt.'], ['M', 'Voeg samen met het vorige document.'], ['R', 'Notitie bij een rode vlag of aandachtspunt: wat is gecontroleerd.'], ['J / K', 'Volgende en vorige in de lijst. Ook met de pijltjes.'], ['← / →', 'Vorige en volgende pagina van het document.'], ['X', 'Uit de stapel halen (dubbel, pakbon, reclame).'], ['U', 'Laatste handeling ongedaan maken.'], ['Z', 'In- en uitzoomen. Klik een veld om er naartoe te zoomen.'], ['E', 'Naar het eerste veld om te corrigeren. Tab gaat naar het volgende.'], ['G', 'Volgorde wisselen: groene golf, risico eerst of stapelvolgorde.'], ['B', 'Naar de ontbrekende bonnen.'], ['Esc', 'Venster sluiten of veld verlaten.']];
  modal(`<h2>Sneltoetsen</h2><table class="keys"><tbody>${k.map(x => `<tr><td>${x[0].split(' / ').map(y => `<kbd>${y}</kbd>`).join(' ')}</td><td>${x[1]}</td></tr>`).join('')}</tbody></table>
    <p class="sub">Groen: alle controles kloppen. Oranje: een punt om te bekijken. Rood: niet goedkeuren zonder notitie.${SIM ? ' De rondleiding speelt een vaste voorbeeldstapel af: het lezen staat vast, de controles worden echt uitgevoerd.' : ''}</p>
    <div class="md-foot"><button class="btn primary" data-close autofocus>Sluiten <kbd>Esc</kbd></button></div>`, 'help').addEventListener('click', e => { if (e.target.closest('[data-close]')) closeModal(); });
}

function finish() {
  const a = docs.filter(d => d.status === 'approved').length, q = docs.filter(d => d.status === 'asked').length, x = docs.filter(d => d.status === 'rejected').length;
  const r = O.run, secs = r && r.start ? ((r.last || Date.now()) - r.start) / 1000 : 0;
  const m = modal(`<h2>De stapel is beoordeeld</h2>
    <div class="fin"><div><b>${a}</b><span>goedgekeurd</span></div><div><b>${q}</b><span>vraag aan klant</span></div><div><b>${x}</b><span>uit de stapel</span></div><div><b>${r && r.n ? fmtSecs(secs) : '–'}</b><span>van eerste tot laatste goedkeuring</span></div></div>
    <label class="fin-in">Hoeveel minuten kost één stuk nu, met de hand? <input id="fin-min" inputmode="decimal" value="${O.manual}"> <span id="fin-out"></span></label>
    <p class="sub">Sorteren, hernoemen, in de juiste map zetten en inboeken. Het getal is uw eigen schatting, niet het onze.</p>
    <div class="md-foot"><button class="btn" data-close>Verder kijken</button><a class="btn primary" href="/afhandelen">Exporteer de batch</a></div>`, 'finish');
  const out = () => { const v = parseNum($('#fin-min', m).value) || 0; O.manual = v || O.manual; if (SIM) save(); $('#fin-out', m).textContent = v ? `= ${Math.round((a + q + x) * v)} minuten handwerk voor deze stapel` : ''; };
  out(); $('#fin-min', m).addEventListener('input', out);
  m.addEventListener('click', e => { if (e.target.closest('[data-close]')) closeModal(); });
}

/* ------------------------------------------------------------------ toasts */
let toastEl = null, toastT = null; const once = {};
function toast(html, kind, long) {
  if (!toastEl) { toastEl = document.createElement('div'); toastEl.className = 'ck-toast'; toastEl.setAttribute('role', 'status'); document.body.appendChild(toastEl); }
  toastEl.className = 'ck-toast show ' + (kind || '');
  toastEl.innerHTML = html;
  clearTimeout(toastT); toastT = setTimeout(() => toastEl.classList.remove('show'), long ? 7000 : 3200);
}
function toastOnce(k, html) { if (!once[k]) { once[k] = 1; toast(html); } }
function shake() { const p = $('.ck-panel'); if (p) { p.classList.remove('shake'); void p.offsetWidth; p.classList.add('shake'); } }

/* ------------------------------------------------------------------ events */
let editT = null;
function fieldChanged(input) {
  const d = byId(S.sel);
  if (!d || d.status !== 'review') return;
  const r = clone(d.result); let changed = null;
  if (input.dataset.k) {
    const k = input.dataset.k, isAmt = k === 'net_total' || k === 'gross_total';
    const v = isAmt ? parseNum(input.value) : (input.value.trim() || null);
    if ((r[k] ?? null) === v) return;
    r[k] = v; changed = FLABEL[k] || k;
  } else if (input.dataset.vat !== undefined) {
    const i = +input.dataset.vat, v = parseNum(input.value);
    if (!r.vat_lines[i] || r.vat_lines[i].amount === v) return;
    r.vat_lines[i].amount = v; changed = 'btw-bedrag';
  } else if (input.dataset.ln) {
    const row = input.closest('[data-line]'), i = +row.dataset.line, k = input.dataset.ln;
    const v = (k === 'amount_excl' || k === 'vat_amount') ? (parseNum(input.value) ?? 0) : input.value;
    if (!r.lines[i] || r.lines[i][k] === v) return;
    r.lines[i][k] = v; changed = 'boekingsregel';
  } else return;
  d.result = r;
  clearTimeout(editT);
  editT = setTimeout(() => saveEdit(d.id, {result: {counterparty: r.counterparty, reference: r.reference, doc_date: r.doc_date, due_date: r.due_date, supplier_vat_number: r.supplier_vat_number, supplier_kvk: r.supplier_kvk, iban: r.iban, description: r.description, net_total: r.net_total, gross_total: r.gross_total, vat_lines: r.vat_lines, lines: r.lines}}, changed), 350);
}

root.addEventListener('click', e => {
  const t = e.target;
  const go = t.closest('[data-go]'); if (go) { e.preventDefault(); return select(go.dataset.go); }
  const q = t.closest('[data-doc]'); if (q) return select(q.dataset.doc, 'doc');
  const ln = t.closest('.q-item[data-line]'); if (ln) return select(ln.dataset.line, 'bank');
  const tab = t.closest('[data-tab]'); if (tab) { S.tab = tab.dataset.tab; return renderQueue(); }
  const jump = t.closest('[data-jump]'); if (jump && !t.closest('a')) { const id = jump.dataset.jump; if (byId(id)) select(id, 'doc'); else if (lineById(id)) select(id, 'bank'); return; }
  const view = t.closest('[data-view]'); if (view) { S.view = view.dataset.view; return renderCanvas(); }
  const pg = t.closest('[data-page]'); if (pg) { S.page = +pg.dataset.page; S.field = null; return renderCanvas(); }
  const sp = t.closest('[data-split]'); if (sp) return split(S.sel, +sp.dataset.split);
  const zm = t.closest('[data-zoom]'); if (zm) return zoom(zm.dataset.zoom);
  const box = t.closest('[data-box]'); if (box) { S.field = box.dataset.box; focusBox(S.field); const i = $(`.fld[data-f="${S.field}"] input`); if (i) { $$('.fld.on').forEach(x => x.classList.remove('on')); i.closest('.fld').classList.add('on'); if (!TOUCH) i.focus({preventScroll: false}); } return; }
  const nt = t.closest('[data-note]'); if (nt) return openNote(nt.dataset.note);
  const un = t.closest('[data-unnote]'); if (un) return removeNote(S.sel, un.dataset.unnote);
  const rm = t.closest('[data-rmline]'); if (rm) { const d = byId(S.sel), r = clone(d.result); r.lines.splice(+rm.dataset.rmline, 1); d.result = r; renderPanel(); return saveEdit(d.id, {result: {lines: r.lines}}, 'boekingsregel'); }
  const act = t.closest('[data-act]');
  if (act) {
    const a = act.dataset.act;
    if (a === 'approve') return approve(S.sel);
    if (a === 'ask') return openAsk();
    if (a === 'note') return openNote();
    if (a === 'reject') return reject(S.sel);
    if (a === 'reopen') return reopen(S.sel);
    if (a === 'help') return openHelp();
    if (a === 'close-line') return closeLine(S.sel, 'Geen bon nodig (bankkosten, privé of al verwerkt)');
    if (a === 'reopen-line') return reopenLine(S.sel);
    if (a === 'demo-reply') return demoReply(S.sel);
    if (a === 'addline') { const d = byId(S.sel), r = clone(d.result), ch = chartOf(d); r.lines = (r.lines || []).concat([{account: (ch[0] || [''])[0], vat_code: 'V21', amount_excl: 0, vat_amount: 0, description: ''}]); d.result = r; return renderPanel(); }
    if (a === 'reset') { if (confirm('De rondleiding opnieuw beginnen? Uw goedkeuringen in deze browser worden gewist.')) { try { localStorage.removeItem(KEY()); } catch (err) { /* nothing stored */ } location.hash = ''; location.reload(); } }
  }
});
root.addEventListener('focusin', e => {
  const f = e.target.closest('.fld');
  if (f && f.dataset.f) { $$('.fld.on').forEach(x => x.classList.remove('on')); f.classList.add('on'); S.field = f.dataset.f; if (S.view === 'doc') focusBox(S.field); }
});
root.addEventListener('change', e => {
  const t = e.target;
  if (t.id === 'q-order') { S.order = t.value; return renderQueue(); }
  if (t.id === 'tp-client') { S.client = t.value; const l = todo(); if (l.length && !l.some(d => d.id === S.sel)) select(l[0].id); else renderAll(); return; }
  if (t.dataset.edit) { const d = byId(S.sel); if (!d) return; const edit = {}; edit[t.dataset.edit] = t.value || null; d[t.dataset.edit] = t.value || null; saveEdit(d.id, edit, t.dataset.edit === 'kind' ? 'soort' : 'dossier').then(() => renderAll()); return; }
  fieldChanged(t);
});
root.addEventListener('input', e => { if (e.target.matches('.fld input[data-k]:not([type=date]), .fld input[data-vat], .ln input')) { clearTimeout(editT); const el = e.target; editT = setTimeout(() => fieldChanged(el), 700); } });
document.addEventListener('click', e => { const go = e.target.closest('.ck-toast [data-go]'); if (go) { e.preventDefault(); select(go.dataset.go); } });

document.addEventListener('keydown', e => {
  if (S.modal) { if (e.key === 'Escape') { e.preventDefault(); closeModal(); } return; }
  const el = document.activeElement, typing = el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT');
  if (typing) {
    if (e.key === 'Escape') { el.blur(); e.preventDefault(); }
    else if (e.key === 'Enter' && el.tagName === 'INPUT') { el.blur(); e.preventDefault(); }
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const map = {
    Enter: () => S.kind === 'doc' ? approve(S.sel) : openAsk(), w: openAsk, s: () => split(S.sel), m: () => mergePrev(S.sel), r: () => openNote(),
    j: () => move(1), k: () => move(-1), ArrowDown: () => move(1), ArrowUp: () => move(-1), x: () => S.kind === 'doc' ? reject(S.sel) : closeLine(S.sel, 'Geen bon nodig'),
    u: undo, z: () => zoom('fit'), '?': openHelp, g: () => { S.order = {wave: 'risk', risk: 'pile', pile: 'wave'}[S.order]; renderQueue(); toast('Volgorde: ' + {wave: 'groene golf eerst', risk: 'risico eerst', pile: 'volgorde van de stapel'}[S.order]); },
    b: () => { const l = missing()[0]; if (l) { S.tab = 'todo'; select(l.id, 'bank'); } },
    e: () => { const i = $('.fld input:not([disabled])'); if (i) i.focus(); },
    ArrowRight: () => page(1), ArrowLeft: () => page(-1), Escape: () => { if (S.zoom > 1) zoom('fit'); },
  };
  if (map[k]) { e.preventDefault(); map[k](); }
});
function page(step) { const d = S.kind === 'doc' ? byId(S.sel) : null; if (!d || d.pages.length < 2) return; const i = Math.max(0, Math.min(d.pages.length - 1, d.pages.indexOf(S.page) + step)); S.page = d.pages[i]; S.field = null; renderCanvas(); }

/* ------------------------------------------------------------------ start */
async function start() {
  try {
    if (SIM) { const r = await fetch('/api/voorbeeld.json', {credentials: 'same-origin'}); D = await r.json(); load(); }
    else { const q = new URLSearchParams(); if (root.dataset.scan) q.set('scan', root.dataset.scan); if (root.dataset.focus) q.set('doc', root.dataset.focus); const r = await fetch('/api/cockpit?' + q, {credentials: 'same-origin'}); D = await r.json(); if (D.manual_minutes) O.manual = D.manual_minutes; }
  } catch (e) { $('.ck-canvas').innerHTML = '<div class="cv-empty"><h2>De werkbank kon niet laden</h2><p>Ververs de pagina. Blijft dit zo, dan is de verbinding weg.</p></div>'; return; }
  build();
  for (const [line, b] of Object.entries(O.bank)) if (b.sent && !b.reply && !b.closed && D.replies[line]) playReply(line); // a reload in the middle of a reply
  const h = decodeURIComponent(location.hash.slice(1));
  const want = root.dataset.focus || (h.startsWith('doc=') ? h.slice(4) : h.startsWith('bon=') ? h.slice(4) : '');
  if (h === 'bonnen') { const l = missing()[0]; if (l) { S.sel = l.id; S.kind = 'bank'; } }
  else if (want && (byId(want) || lineById(want))) { S.sel = want; S.kind = byId(want) ? 'doc' : 'bank'; }
  if (!S.sel) { const t = todo(); if (t.length) S.sel = t[0].id; }
  if (S.sel) select(S.sel, S.kind); else renderAll();
  clock = setInterval(() => { const r = O.run; if (r && r.n && r.last && Date.now() - r.last < 4000) { const el = $('.tp-run .mono'); if (el) el.textContent = fmtSecs((Date.now() - r.start) / 1000); } }, 100);
  window.addEventListener('resize', fit);
  window.addEventListener('hashchange', () => { const x = decodeURIComponent(location.hash.slice(1)); if (x === 'bonnen') { const l = missing()[0]; if (l) select(l.id, 'bank'); } });
  if (SIM && !O.hints.welcome) { O.hints.welcome = 1; save(); setTimeout(() => toast('Druk op <kbd>Enter</kbd> om het eerste document goed te keuren. <kbd>?</kbd> toont alle sneltoetsen.', '', true), 600); }
}
start();
})();
