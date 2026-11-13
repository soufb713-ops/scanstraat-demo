/* Sandbox on the homepage. A pdf with a text layer is read here, in the browser, with fixed rules: no AI and no upload.
   It shows the fields with their place on the page, checks the VAT arithmetic, suggests a ledger account and builds
   an SI-UBL 2.0 (NLCIUS) concept with the same rule checks as the export in the product (ubl.py).
   Photos and scans are not read here; the receipt sample shows the fixed outcome of that one example and says so. */
(() => {
'use strict';
const root = document.getElementById('sb');
if (!root) return;
const $ = (s, r = root) => r.querySelector(s), $$ = (s, r = root) => [...r.querySelectorAll(s)];
const BASE = root.dataset.base, PDFJS = root.dataset.pdfjs;
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const cents = v => Math.round((+v || 0) * 100);
const nlc = c => (c / 100).toLocaleString('nl-NL', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const nl = v => (v == null || v === '' || isNaN(v)) ? '' : nlc(cents(v));
const dec = c => (c / 100).toFixed(2);
const els = {name: $('#sb-name'), meta: $('#sb-meta'), empty: $('#sb-empty'), sheet: $('#sb-sheet'), pager: $('#sb-pager'), fields: $('#sb-fields'), ubl: $('#sb-ubl'), drop: $('#sb-drop'), file: $('#sb-file')};

const FIELDS = [   // key, label, colour class (the same colours as in the workbench)
  ['supplier', 'Leverancier', 'party'], ['number', 'Factuurnummer', 'ref'], ['date', 'Factuurdatum', 'date'], ['due', 'Vervaldatum', 'due'],
  ['vat_id', 'Btw-id', 'id'], ['kvk', 'KvK', 'id'], ['iban', 'IBAN', 'iban'], ['customer', 'Afnemer', 'party']];
const RATE_CLS = r => r >= 15 ? 'v21' : r > 0 ? 'v9' : 'v0';
const LABEL = Object.fromEntries(FIELDS.map(f => [f[0], f[1]]));
const CLS = Object.fromEntries(FIELDS.map(f => [f[0], f[2]]));

/* ---------------------------------------------------------------- the three samples */
const SAMPLES = {
  bon: {name: 'kassabon-eetcafe.jpg', image: 'kassabon-eetcafe.jpg',
    note: 'Foto: dit is de vaste uitkomst van dit ene voorbeeld. Een foto lezen we in deze browsertest niet live; bij uw kantoor doet de leesmotor dat op de server.',
    f: {supplier: "Eetcafe 't Anker", number: '004512', date: '2026-11-07', due: '', vat_id: '', kvk: '24550817', iban: '', customer: '',
        vat: [{rate: 9, base: 28.53, amount: 2.57}, {rate: 21, base: 13.80, amount: 2.90}], gross: 47.80},
    boxes: {supplier: [0, [.245, .215, .67, .27]], kvk: [0, [.352, .288, .562, .323]], number: [0, [.436, .334, .614, .362]], date: [0, [.296, .354, .466, .384]],
            gross: [0, [.636, .574, .73, .603]], 'b9': [0, [.332, .636, .422, .662]], 'a9': [0, [.47, .636, .553, .664]], 'b21': [0, [.334, .664, .424, .69]], 'a21': [0, [.472, .664, .553, .692]]},
    gl: {acc: ['4520', 'Representatiekosten', 'WBedVkkRep'], why: 'Horecabon met tafelnummer: een zakelijke lunch of diner.',
         notes: [['', 'Horecabesteding: de btw op eten en drinken in de horeca is niet aftrekbaar. Voorstel: btw niet terugvragen.'], ['ok', 'Twee tarieven op één bon: 9% en 21% apart nagerekend.']]}},
  groothandel: {name: 'factuur-groothandel-9-21.pdf', pdf: 'factuur-groothandel-9-21.pdf'},
  eu: {name: 'invoice-reverse-charge.pdf', pdf: 'invoice-reverse-charge.pdf'}
};

/* ---------------------------------------------------------------- reading numbers, dates, IBANs */
function num(s) {
  s = String(s == null ? '' : s).replace(/[^\d.,-]/g, '');
  if (!/\d/.test(s)) return null;
  const neg = s.charAt(0) === '-'; s = s.replace(/-/g, '');
  const i = Math.max(s.lastIndexOf(','), s.lastIndexOf('.'));
  let v;
  if (i === -1) v = parseFloat(s);
  else {
    const frac = s.slice(i + 1), other = s[i] === ',' ? '.' : ',';
    if (frac.length === 3 && s.indexOf(other) === -1 && s.indexOf(s[i]) === i && s.length > 4) v = parseFloat(s.replace(/[.,]/g, ''));   // 1.250 = thousand
    else v = parseFloat(s.slice(0, i).replace(/[.,]/g, '') + '.' + frac);
  }
  return isNaN(v) ? null : (neg ? -v : v);
}
function amounts(text) {     // every amount with two decimals in a line: [{v, i, len}]
  const re = /(^|[^\d.,])(-?(?:\d{1,3}(?:[.  ]\d{3})+|\d+),\d{2}|-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})(?!\d)/g, out = [];
  let m;
  while ((m = re.exec(text))) out.push({v: num(m[2]), i: m.index + m[1].length, len: m[2].length});
  return out;
}
const MON = {jan: 1, januari: 1, january: 1, feb: 2, februari: 2, february: 2, mrt: 3, maart: 3, mar: 3, march: 3, apr: 4, april: 4, mei: 5, may: 5, jun: 6, juni: 6, june: 6,
  jul: 7, juli: 7, july: 7, aug: 8, augustus: 8, august: 8, sep: 9, sept: 9, september: 9, okt: 10, oktober: 10, oct: 10, october: 10, nov: 11, november: 11, dec: 12, december: 12};
const DATE = '(\\d{4}-\\d{2}-\\d{2}|\\d{1,2}[-/.]\\d{1,2}[-/.]\\d{2,4}|\\d{1,2}\\s+[A-Za-z]{3,9}\\.?\\s+\\d{4})';
function iso(s) {
  const ok = (y, m, d) => (+m >= 1 && +m <= 12 && +d >= 1 && +d <= 31) ? `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}` : '';
  let m;
  if ((m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s))) return ok(m[1], m[2], m[3]);
  if ((m = /^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$/.exec(s))) return ok(m[3].length === 2 ? '20' + m[3] : m[3], m[2], m[1]);
  if ((m = /^(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})$/.exec(s))) { const mo = MON[m[2].toLowerCase()]; return mo ? ok(m[3], mo, m[1]) : ''; }
  return '';
}
const IBAN_LEN = {NL: 18, BE: 16, DE: 22, FR: 27, IE: 22, GB: 22, ES: 24, IT: 27, LU: 20, AT: 20, PT: 25, DK: 18, SE: 24, FI: 18, PL: 28, CH: 21};
function ibanOk(v) {
  const s = String(v || '').replace(/\s/g, '').toUpperCase();
  if (!/^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$/.test(s) || (IBAN_LEN[s.slice(0, 2)] && IBAN_LEN[s.slice(0, 2)] !== s.length)) return false;
  let r = 0;
  for (const ch of s.slice(4) + s.slice(0, 4)) for (const d of String(parseInt(ch, 36))) r = (r * 10 + +d) % 97;
  return r === 1;
}
const dmy = s => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || ''); return m ? `${m[3]}-${m[2]}-${m[1]}` : ''; };
const group4 = s => String(s || '').replace(/\s/g, '').replace(/(.{4})/g, '$1 ').trim();

/* ---------------------------------------------------------------- pdf: text with positions */
let pdfjs;
function loadPdfJs() {
  return pdfjs || (pdfjs = new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = PDFJS + 'pdf.min.js';
    s.onload = () => { window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS + 'pdf.worker.min.js'; res(window.pdfjsLib); };
    s.onerror = () => { pdfjs = null; rej(new Error('pdf.js niet geladen')); };
    document.head.append(s);
  }));
}
function toLines(items) {
  const rows = [];
  for (const it of items.slice().sort((a, b) => a.y - b.y || a.x - b.x)) {
    const row = rows.find(r => Math.abs(r.y - it.y) <= Math.max(2.5, Math.min(r.h, it.h) * .45));
    if (row) { row.items.push(it); row.h = Math.max(row.h, it.h); } else rows.push({y: it.y, h: it.h, items: [it]});
  }
  for (const r of rows) {
    r.items.sort((a, b) => a.x - b.x); r.text = ''; r.map = [];
    for (const it of r.items) {
      if (r.text && !/\s$/.test(r.text) && !/^\s/.test(it.s)) r.text += ' ';
      r.map.push({start: r.text.length, end: r.text.length + it.s.length, item: it}); r.text += it.s;
    }
  }
  return rows.sort((a, b) => a.y - b.y);
}
function boxOf(pg, line, start, end) {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const m of line.map) {
    if (m.end <= start || m.start >= end) continue;
    const it = m.item, n = Math.max(1, it.s.length);
    x0 = Math.min(x0, it.x + it.w * (Math.max(start, m.start) - m.start) / n); x1 = Math.max(x1, it.x + it.w * (Math.min(end, m.end) - m.start) / n);
    y0 = Math.min(y0, it.y - it.h * .92); y1 = Math.max(y1, it.y + it.h * .3);
  }
  if (!isFinite(x0)) return null;
  const p = 1.6;
  return [Math.max(0, (x0 - p) / pg.w), Math.max(0, (y0 - p) / pg.h), Math.min(1, (x1 + p) / pg.w), Math.min(1, (y1 + p) / pg.h)];
}
async function openPdf(bytes) {
  const lib = await loadPdfJs();
  const doc = await lib.getDocument({data: bytes, standardFontDataUrl: PDFJS + 'standard_fonts/', isEvalSupported: false, useSystemFonts: false}).promise;
  const pages = [], total = doc.numPages;
  for (let n = 1; n <= Math.min(total, 5); n++) {
    const page = await doc.getPage(n), vp = page.getViewport({scale: 1});
    const tc = await page.getTextContent();
    const items = tc.items.filter(i => i.str && i.str.trim()).map(i => {
      const t = lib.Util.transform(vp.transform, i.transform);
      return {s: i.str, x: t[4], y: t[5], w: i.width, h: Math.hypot(t[2], t[3]) || i.height || 10};
    });
    const scale = Math.min(2.4, 1120 / vp.width), v2 = page.getViewport({scale});
    const canvas = document.createElement('canvas'); canvas.width = Math.round(v2.width); canvas.height = Math.round(v2.height);
    await page.render({canvasContext: canvas.getContext('2d'), viewport: v2}).promise;
    pages.push({w: vp.width, h: vp.height, items, lines: toLines(items), el: canvas});
  }
  return {pages, total};
}

/* ---------------------------------------------------------------- pdf: the rules that find the fields */
const LEGAL = /\b(?:B\.?V\.?|N\.?V\.?|V\.?O\.?F\.?|C\.?V\.?|Ltd\.?|Limited|GmbH|Inc\.?|LLC|S\.?A\.?R?\.?L?\.?|BVBA|SRL|Holding)(?=$|[\s,.)])/;
function parse(pages) {
  const f = {supplier: '', number: '', date: '', due: '', vat_id: '', kvk: '', iban: '', customer: '', vat: [], gross: null}, boxes = {};
  const all = [];
  pages.forEach((pg, pi) => pg.lines.forEach((ln, li) => all.push({pg, pi, ln, li})));
  const text = all.map(a => a.ln.text).join('\n');
  const put = (key, value, a, start, end) => { f[key] = value; const b = boxOf(a.pg, a.ln, start, end); if (b) boxes[key] = [a.pi, b]; };
  const grab = (key, re, {skip, fix = v => v, test = () => true} = {}) => {
    for (const a of all) {
      if (skip && skip.test(a.ln.text)) continue;
      const m = re.exec(a.ln.text);
      if (!m) continue;
      const raw = m[m.length - 1], v = fix(raw);
      if (!v || !test(v)) continue;
      const start = m.index + m[0].lastIndexOf(raw);
      put(key, v, a, start, start + raw.length);
      return true;
    }
    return false;
  };
  const DUE = /vervaldatum|verval|uiterste\s+betaal|due\s+date|payment\s+due|betalen\s+v[oó]{2}r|te\s+voldoen\s+v[oó]{2}r/i;
  grab('number', /(?:factuur\s*(?:nummer|nr|no)\.?|invoice\s*(?:number|no\.?|nr\.?|#)|kenmerk|bon(?:nummer)?)\s*[:#]?\s*([A-Z0-9][A-Z0-9\-\/.]{2,24})/i, {test: v => /\d/.test(v)});
  grab('due', new RegExp('(?:' + DUE.source + ')\\s*:?\\s*' + DATE, 'i'), {fix: iso});
  grab('date', new RegExp('(?:^|[^a-z])(?:factuurdatum|invoice\\s+date|datum|date)\\s*:?\\s*' + DATE, 'i'), {skip: DUE, fix: iso}) ||
    grab('date', new RegExp(DATE), {skip: DUE, fix: iso});
  grab('kvk', /(?:kvk|k\.v\.k\.?|kamer van koophandel|coc)\D{0,10}(\d{8})(?!\d)/i);
  const CUST = /customer|klant|afnemer|uw\s+btw|your\s+vat|client/i;
  const VATID = /\b((?:NL\d{9}B\d{2})|(?:AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|HR|HU|IE|IT|LT|LU|LV|MT|PL|PT|RO|SE|SI|SK|XI|GB)[0-9A-Z]{8,12})\b/;
  grab('vat_id', VATID, {skip: CUST, test: v => /\d{5}/.test(v)});
  iban: for (const a of all) {                                    // the first bank account that passes the check digits
    const re = /\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?)\b/g;
    let m;
    while ((m = re.exec(a.ln.text))) if (ibanOk(m[1])) { put('iban', m[1].replace(/\s/g, ''), a, m.index, m.index + m[1].length); break iban; }
  }
  // names: the largest line at the top of page one is the sender; the next name with a legal form is the buyer
  const top = pages[0] ? pages[0].lines.filter(l => l.y < pages[0].h * .4 && /[A-Za-z]{3}/.test(l.text) && !/^\s*(factuur|invoice|creditnota|credit\s*note|pro\s*forma|rekening|nota)\b/i.test(l.text)) : [];
  let sup = top.slice().sort((x, y) => y.h - x.h || x.y - y.y)[0];
  const firstLegal = all.find(a => LEGAL.test(a.ln.text));
  if (sup && firstLegal && firstLegal.pi === 0 && firstLegal.ln.y < sup.y && sup.h - firstLegal.ln.h < 1) sup = firstLegal.ln;
  if (sup) {
    const m = LEGAL.exec(sup.text), name = (m ? sup.text.slice(0, m.index + m[0].length) : sup.text).trim().slice(0, 70);
    put('supplier', name, {pg: pages[0], pi: 0, ln: sup}, sup.text.indexOf(name), sup.text.indexOf(name) + name.length);
  }
  for (const a of all) {
    const m = LEGAL.exec(a.ln.text);
    if (!m || a.ln === sup || (f.supplier && a.ln.text.toLowerCase().includes(f.supplier.toLowerCase()))) continue;
    const cut = a.ln.text.slice(0, m.index + m[0].length), start = Math.max(0, cut.search(/[A-ZÀ-Ý][^:]*$/));
    const name = cut.slice(start).trim();
    if (name.length > 3 && name.length < 70) { put('customer', name, a, start, m.index + m[0].length); break; }
  }

  // amounts: the labelled totals first, then the VAT lines per rate
  const amountAt = (res, not) => {
    for (const re of res) for (let k = all.length - 1; k >= 0; k--) {
      const a = all[k], m = re.exec(a.ln.text);
      if (!m || (not && not.test(a.ln.text))) continue;
      let line = a.ln, am = amounts(line.text).filter(x => x.i >= m.index);
      if (!am.length && a.pg.lines[a.li + 1]) { line = a.pg.lines[a.li + 1]; am = amounts(line.text); }
      if (!am.length) continue;
      const x = am[am.length - 1];
      return {v: x.v, box: [a.pi, boxOf(a.pg, line, x.i, x.i + x.len)]};
    }
    return null;
  };
  const gross = amountAt([/te\s+betalen|totaal\s+te\s+voldoen|te\s+voldoen|total\s+due|amount\s+due|balance\s+due|totaal\s*bedrag|invoice\s+total|grand\s+total|totaal\s+incl|total\s+incl|totaal\s+factuur/i,
                          /\b(?:totaal|total)\b/i], /excl|sub\s*tota|korting|discount|grondslag/i);
  const net = amountAt([/totaal\s+excl|total\s+excl|sub\s*tota+l|netto\s*bedrag|net\s+amount|bedrag\s+excl|exclusief\s+btw/i]);
  const rates = {};
  for (const a of all) {
    const t = a.ln.text, rm = /(\d{1,2}(?:[.,]\d)?)\s*%/.exec(t);
    if (!rm) continue;
    const rate = num(rm[1]), am = amounts(t), table = /^\s*\d{1,2}(?:[.,]\d)?\s*%/.test(t) && am.length >= 2;
    if (rate > 27 || !am.length || (!table && !/btw|b\.t\.w|vat|tax|grondslag|maatstaf|omzetbelasting|tva|mwst/i.test(t))) continue;
    const r = rates[rate] || (rates[rate] = {rate}), bx = x => [a.pi, boxOf(a.pg, a.ln, x.i, x.i + x.len)];
    if (am.length >= 2) { r.base = am[0].v; r.bb = bx(am[0]); r.amount = am[1].v; r.ab = bx(am[1]); }
    else if (/grondslag|maatstaf|taxable|basis/i.test(t) && !/^\s*(?:btw|vat)\b/i.test(t)) { r.base = am[0].v; r.bb = bx(am[0]); }
    else { r.amount = am[0].v; r.ab = bx(am[0]); }
  }
  let vat = Object.values(rates).sort((x, y) => y.rate - x.rate);
  const known = () => vat.reduce((s, v) => s + (v.base != null ? cents(v.base) : 0), 0);
  for (const v of vat) {
    if (v.base == null && v.amount != null) {
      if (vat.length === 1 && net) v.base = net.v;
      else if (v.rate === 0 && net) v.base = (cents(net.v) - known()) / 100;
      else if (v.rate > 0) { v.base = Math.round(cents(v.amount) * 100 / v.rate) / 100; v.derived = true; }
      else if (gross) v.base = (cents(gross.v) - known() - vat.reduce((s, x) => s + cents(x.amount), 0)) / 100;
    }
    if (v.amount == null && v.base != null) v.amount = Math.round(cents(v.base) * v.rate / 100) / 100;
  }
  if (!vat.length && net && gross) {
    const diff = cents(gross.v) - cents(net.v), fit = [21, 9, 0].find(r => Math.abs(Math.round(cents(net.v) * r / 100) - diff) <= 2);
    vat = [{rate: fit != null ? fit : Math.round(diff / cents(net.v) * 1000) / 10, base: net.v, amount: diff / 100, bb: net.box}];
  }
  vat = vat.filter(v => v.base != null);
  f.vat = vat.map(v => ({rate: v.rate, base: v.base, amount: v.amount || 0}));
  vat.forEach(v => { if (v.bb && v.bb[1]) boxes['b' + v.rate] = v.bb; if (v.ab && v.ab[1]) boxes['a' + v.rate] = v.ab; });
  if (gross) { f.gross = gross.v; if (gross.box[1]) boxes.gross = gross.box; }
  else if (f.vat.length) f.gross = f.vat.reduce((s, v) => s + cents(v.base) + cents(v.amount), 0) / 100;
  f.reverse = /btw\s+verlegd|verlegd|reverse[\s-]?charge[ds]?|autoliquidation|article\s+196|artikel\s+196|art\.?\s*12\s+lid\s+2/i.test(text);
  return {f, boxes, text, found: Object.keys(boxes).length};
}

/* ---------------------------------------------------------------- ledger suggestion (rules on words, a proposal only) */
const GL = [
  [/groothandel|wholesale|handelsgoederen|cash\s*&\s*carry/i, ['7010', 'Inkoop handelsgoederen', 'WKprInhInh'], 'Groothandel: inkoop voor de verkoop.'],
  [/software|subscription|abonnement|licen[sc]|saas|hosting|cloud|domein|domain/i, ['4330', 'Software en abonnementen', 'WBedKanSof'], 'Software of abonnement.'],
  [/brandstof|benzine|diesel|tankstation|fuel|laadpaal|laadsessie/i, ['4410', 'Brandstof', 'WBedAutBra'], 'Brandstof of laden.'],
  [/telefoon|internet|mobiel|telecom|glasvezel/i, ['4310', 'Telefoon en internet', 'WBedKanTef'], 'Telefonie of internet.'],
  [/energie|stroom|elektra|aardgas|drinkwater/i, ['4200', 'Gas, water en elektra', 'WBedHuiGwe'], 'Energie of water.'],
  [/verzekering|polis|insurance/i, ['4700', 'Verzekeringen bedrijf', 'WBedAssBea'], 'Verzekeringspremie.'],
  [/advertentie|reclame|marketing|drukwerk|campagne/i, ['4600', 'Reclame- en advertentiekosten', 'WBedVkkRea'], 'Reclame of advertentie.'],
  [/parkeer|parking/i, ['4420', 'Parkeerkosten', 'WBedAutPar'], 'Parkeren.'],
  [/restaurant|eetcaf[eé]|brasserie|lunch|diner|bistro|horeca/i, ['4520', 'Representatiekosten', 'WBedVkkRep'], 'Horecabesteding.'],
  [/bouwmarkt|bouwmaterialen|gereedschap|ijzerwaren/i, ['7000', 'Inkoop grond- en hulpstoffen', 'WKprKvgKvg'], 'Materiaal voor het werk.'],
  [/kantoorartikelen|kantoorbenodigdheden|papier|toner|office\s+supplies/i, ['4320', 'Kantoorbenodigdheden', 'WBedKanKan'], 'Kantoorartikelen.'],
  [/huur|rent\b/i, ['4100', 'Huur bedrijfsruimte', 'WBedHuiBeh'], 'Huur.']];
function suggest(S) {
  if (S.gl) return S.gl;
  const f = S.f, hay = (f.supplier || '') + '\n' + (S.text || '');
  const hit = hay.trim() ? GL.find(g => g[0].test(hay)) : null, notes = [];
  const vatNo = (f.vat_id || '').replace(/\s/g, '').toUpperCase(), foreign = vatNo && !vatNo.startsWith('NL');
  const noVat = !f.vat.some(v => cents(v.amount));
  if ((S.reverse || foreign) && noVat && f.vat.length) notes.push(['', foreign
    ? `Btw verlegd, leverancier in het buitenland (${vatNo.slice(0, 2)}). Voorstel voor de btw-code: verwerving binnen de EU, zelf aangeven en als voorbelasting terugvragen.`
    : 'Btw verlegd: geen btw op de factuur. Voorstel voor de btw-code: verlegd, zelf aangeven.']);
  if (f.vat.filter(v => v.rate > 0).length > 1) notes.push(['ok', 'Meer tarieven op één stuk: elk tarief apart nagerekend en als eigen regel in het SI-UBL-bestand.']);
  if (f.iban) notes.push(ibanOk(f.iban) ? ['ok', 'IBAN: controlegetal klopt. In de werkbank wordt het ook vergeleken met eerdere boekingen van deze crediteur.'] : ['', 'IBAN: het controlegetal klopt niet. Lees het nummer na op het document.']);
  if (f.vat.some(v => v.derived)) notes.push(['', 'Bij een tarief stond geen grondslag op het stuk: die is teruggerekend uit het btw-bedrag.']);
  return {acc: hit ? hit[1] : ['4900', 'Algemene kosten', 'WBedAlkOal'],
          why: hit ? hit[2] + ' Gekozen op woorden in het document; in de werkbank telt zwaarder wat u eerder voor deze crediteur boekte.'
                   : 'Geen duidelijke aanwijzing in de tekst. In de werkbank telt wat u eerder voor deze crediteur boekte.', notes};
}

/* ---------------------------------------------------------------- SI-UBL 2.0 (NLCIUS), built like ubl.py */
const NS = {cbc: 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2', cac: 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'};
function buildUbl(S, embed) {
  const f = S.f, lines = f.vat.length ? f.vat : [{rate: 0, base: f.gross, amount: 0}];
  const vatNo = (f.vat_id || '').replace(/\s/g, '').toUpperCase();
  const foreignReverse = !!vatNo && !vatNo.startsWith('NL') && !lines.some(v => cents(v.amount));
  const cat = r => foreignReverse ? 'AE' : (cents(r) === 0 ? 'Z' : 'S');
  const number = f.number || `BON-${f.date || 'onbekend'}`, desc = S.gl ? S.gl.acc[1] : suggest(S).acc[1];
  let net = 0, tax = 0, subs = '', rows = '';
  lines.forEach((v, i) => {
    const b = cents(v.base), a = cents(v.amount), c = cat(v.rate), pct = (+v.rate || 0).toFixed(2);
    net += b; tax += a;
    subs += `
    <cac:TaxSubtotal>
      <cbc:TaxableAmount currencyID="EUR">${dec(b)}</cbc:TaxableAmount>
      <cbc:TaxAmount currencyID="EUR">${dec(a)}</cbc:TaxAmount>
      <cac:TaxCategory><cbc:ID>${c}</cbc:ID><cbc:Percent>${pct}</cbc:Percent>${c === 'AE' ? '<cbc:TaxExemptionReason>Btw verlegd</cbc:TaxExemptionReason>' : ''}<cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory>
    </cac:TaxSubtotal>`;
    rows += `
  <cac:InvoiceLine>
    <cbc:ID>${i + 1}</cbc:ID>
    <cbc:InvoicedQuantity unitCode="C62">1</cbc:InvoicedQuantity>
    <cbc:LineExtensionAmount currencyID="EUR">${dec(b)}</cbc:LineExtensionAmount>
    <cac:Item><cbc:Name>${esc(desc)}</cbc:Name>
      <cac:ClassifiedTaxCategory><cbc:ID>${c}</cbc:ID><cbc:Percent>${pct}</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory>
    </cac:Item>
    <cac:Price><cbc:PriceAmount currencyID="EUR">${dec(b)}</cbc:PriceAmount></cac:Price>
  </cac:InvoiceLine>`;
  });
  const gross = f.gross != null && f.gross !== '' ? cents(f.gross) : net + tax, iban = (f.iban || '').replace(/\s/g, '');
  const att = S.pdf ? `
  <cac:AdditionalDocumentReference>
    <cbc:ID>${esc(S.name)}</cbc:ID>
    <cac:Attachment><cbc:EmbeddedDocumentBinaryObject mimeCode="application/pdf" filename="${esc(S.name)}">${embed ? b64(S.pdf) : `[pdf-bijlage, ${Math.max(1, Math.round(S.pdf.length / 1024))} kB als base64: volledig in de download]`}</cbc:EmbeddedDocumentBinaryObject></cac:Attachment>
  </cac:AdditionalDocumentReference>` : '';
  const legal = `<cbc:RegistrationName>${esc(f.supplier)}</cbc:RegistrationName>` + (f.kvk ? `<cbc:CompanyID schemeID="0106">${esc(f.kvk)}</cbc:CompanyID>` : '');
  const country = /^[A-Z]{2}/.test(vatNo) ? vatNo.slice(0, 2) : 'NL';
  return `<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" xmlns:cac="${NS.cac}" xmlns:cbc="${NS.cbc}">
  <cbc:CustomizationID>urn:cen.eu:en16931:2017#compliant#urn:fdc:nen.nl:nlcius:v1.0</cbc:CustomizationID>
  <cbc:ProfileID>urn:fdc:peppol.eu:2017:poacc:billing:01:1.0</cbc:ProfileID>
  <cbc:ID>${esc(number)}</cbc:ID>
  <cbc:IssueDate>${esc(f.date)}</cbc:IssueDate>${f.due ? `\n  <cbc:DueDate>${esc(f.due)}</cbc:DueDate>` : ''}
  <cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>
  <cbc:Note>CONCEPT uit de test op de website: door niemand goedgekeurd, niet importeren.</cbc:Note>
  <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>${att}
  <cac:AccountingSupplierParty><cac:Party>
    <cac:PartyName><cbc:Name>${esc(f.supplier)}</cbc:Name></cac:PartyName>
    <cac:PostalAddress><cac:Country><cbc:IdentificationCode>${country}</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
    ${vatNo ? `<cac:PartyTaxScheme><cbc:CompanyID>${esc(vatNo)}</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>` : ''}
    <cac:PartyLegalEntity>${legal}</cac:PartyLegalEntity>
  </cac:Party></cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty><cac:Party>
    <cac:PartyName><cbc:Name>${esc(f.customer)}</cbc:Name></cac:PartyName>
    <cac:PostalAddress><cac:Country><cbc:IdentificationCode>NL</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
    <cac:PartyLegalEntity><cbc:RegistrationName>${esc(f.customer)}</cbc:RegistrationName></cac:PartyLegalEntity>
  </cac:Party></cac:AccountingCustomerParty>${iban ? `
  <cac:PaymentMeans><cbc:PaymentMeansCode>58</cbc:PaymentMeansCode><cac:PayeeFinancialAccount><cbc:ID>${esc(iban)}</cbc:ID></cac:PayeeFinancialAccount></cac:PaymentMeans>` : ''}
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="EUR">${dec(tax)}</cbc:TaxAmount>${subs}
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="EUR">${dec(net)}</cbc:LineExtensionAmount>
    <cbc:TaxExclusiveAmount currencyID="EUR">${dec(net)}</cbc:TaxExclusiveAmount>
    <cbc:TaxInclusiveAmount currencyID="EUR">${dec(gross)}</cbc:TaxInclusiveAmount>
    <cbc:PayableAmount currencyID="EUR">${dec(gross)}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>${rows}
</Invoice>
`;
}
function b64(bytes) { let s = ''; for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000)); return btoa(s); }
function verify(xml) {     // the arithmetic rules of EN 16931 and the core fields, checked on the file itself
  const doc = new DOMParser().parseFromString(xml, 'application/xml'), top = doc.documentElement, out = [];
  const kid = (n, seg) => { const [p, name] = seg.split(':'); return n ? [...n.children].filter(c => c.namespaceURI === NS[p] && c.localName === name) : []; };
  const at = (path, n = top) => path.split('/').reduce((x, seg) => kid(x, seg)[0], n);
  const text = path => { const e = at(path); return e ? e.textContent.trim() : ''; };
  const amt = (path, n = top) => { const e = at(path, n); return e && /^-?\d+(\.\d+)?$/.test(e.textContent.trim()) ? Math.round(parseFloat(e.textContent) * 100) : null; };
  const rule = (code, label, ok, detail) => out.push({rule: code, text: label, ok: !!ok, detail});
  if (doc.getElementsByTagName('parsererror').length) { rule('XML', 'Het bestand is geldige XML', false, 'Een veld bevat tekens die het bestand breken.'); return out; }
  const core = {'factuurnummer (BT-1)': text('cbc:ID'), 'factuurdatum (BT-2)': text('cbc:IssueDate'), 'valuta (BT-5)': text('cbc:DocumentCurrencyCode'),
    'leverancier (BT-27)': text('cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName'),
    'afnemer (BT-44)': text('cac:AccountingCustomerParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName')};
  const missing = Object.keys(core).filter(k => !core[k]);
  rule('Kernvelden', 'Factuurnummer, datum, valuta, leverancier en afnemer zijn gevuld', !missing.length, missing.length ? 'Ontbreekt: ' + missing.join(', ') : 'Alle vijf aanwezig.');
  rule('Profiel', 'CustomizationID is NLCIUS (de Nederlandse invulling van EN 16931, in UBL: SI-UBL 2.0)', text('cbc:CustomizationID').endsWith('nlcius:v1.0'), text('cbc:CustomizationID') || 'ontbreekt');
  const lines = kid(top, 'cac:InvoiceLine').map(l => amt('cbc:LineExtensionAmount', l));
  const subs = kid(at('cac:TaxTotal'), 'cac:TaxSubtotal').map(s => [amt('cbc:TaxableAmount', s), amt('cbc:TaxAmount', s), parseFloat((at('cac:TaxCategory/cbc:Percent', s) || {}).textContent)]);
  const lineSum = amt('cac:LegalMonetaryTotal/cbc:LineExtensionAmount'), excl = amt('cac:LegalMonetaryTotal/cbc:TaxExclusiveAmount'), incl = amt('cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount');
  const due = amt('cac:LegalMonetaryTotal/cbc:PayableAmount'), tax = amt('cac:TaxTotal/cbc:TaxAmount'), sum = a => a.reduce((s, x) => s + x, 0);
  if (!lines.length || lines.includes(null) || [lineSum, excl, incl, due, tax].includes(null)) { rule('Bedragen', 'Alle totalen zijn leesbaar', false, 'Een van de totaalbedragen ontbreekt.'); return out; }
  rule('BR-CO-10', 'Som van de regels = totaal van de regels', sum(lines) === lineSum, `${lines.map(nlc).join(' + ')} = ${nlc(sum(lines))}; in het bestand ${nlc(lineSum)}`);
  rule('BR-CO-13', 'Totaal excl. btw = som van de regels (geen kortingen of toeslagen)', excl === lineSum, `${nlc(excl)} tegenover ${nlc(lineSum)}`);
  rule('BR-CO-14', 'Totaal btw = som van de btw per tarief', sum(subs.map(s => s[1])) === tax, `${subs.map(s => nlc(s[1])).join(' + ') || '0,00'} = ${nlc(sum(subs.map(s => s[1])))}; in het bestand ${nlc(tax)}`);
  rule('BR-CO-15', 'Totaal incl. btw = totaal excl. btw + btw', excl + tax === incl, `${nlc(excl)} + ${nlc(tax)} = ${nlc(excl + tax)}; in het bestand ${nlc(incl)}`);
  rule('BR-CO-16', 'Te betalen = totaal incl. btw (niets vooruitbetaald)', due === incl, `${nlc(due)} tegenover ${nlc(incl)}`);
  const calc = s => Math.round(s[0] * s[2] / 100), bad = subs.filter(s => s.some(x => x == null || isNaN(x)) || Math.abs(calc(s) - s[1]) > 1);
  rule('BR-CO-17', 'Btw per tarief = grondslag x tarief, afgerond op de cent', !bad.length,
       subs.filter(s => !s.some(x => x == null || isNaN(x))).map(s => `${String(s[2]).replace('.', ',')}% van ${nlc(s[0])} = ${nlc(calc(s))}` + (bad.includes(s) ? `, in het bestand ${nlc(s[1])}` : '')).join('; '));
  return out;
}
function hl(xml) {
  return esc(xml).split('\n').map(line => '<span class="l">' + line
    .replace(/(&lt;\/?)([\w:.-]+)/g, '$1<span class="t">$2</span>')
    .replace(/([\w:.-]+)=(&quot;.*?&quot;)/g, '<span class="a">$1</span>=<span class="v">$2</span>') + '</span>').join('');
}

/* ---------------------------------------------------------------- the screen */
let S = null, tab = 'fields', seq = 0;
function setTab(t) {
  tab = t;
  $$('.sb-tabs button').forEach(b => b.setAttribute('aria-selected', b.dataset.t === t));
  els.fields.hidden = t !== 'fields'; els.ubl.hidden = t !== 'ubl';
}
function drawSheet() {
  const pg = S.pages[S.page];
  els.empty.hidden = true; els.sheet.hidden = false;
  els.sheet.replaceChildren(pg.el);
  for (const [k, b] of Object.entries(S.boxes)) {
    if (b[0] !== S.page || !b[1]) continue;
    const r = /^[ab](\d+(?:\.\d+)?)$/.exec(k), cls = r ? RATE_CLS(+r[1]) : k === 'gross' ? 'gross' : CLS[k] || 'party';
    const label = r ? (k[0] === 'b' ? 'Grondslag ' : 'Btw ') + String(+r[1]).replace('.', ',') + '%' : k === 'gross' ? 'Totaal' : LABEL[k] || k;
    const el = document.createElement('button');
    el.type = 'button'; el.className = 'box f-' + cls; el.dataset.box = k;
    el.style.cssText = `left:${b[1][0] * 100}%;top:${b[1][1] * 100}%;width:${(b[1][2] - b[1][0]) * 100}%;height:${(b[1][3] - b[1][1]) * 100}%`;
    el.innerHTML = `<span>${esc(label)}</span>`;
    els.sheet.append(el);
  }
  placeLabels();
  els.pager.hidden = S.pages.length < 2;
  if (S.pages.length > 1) els.pager.innerHTML = S.pages.map((_, i) => `<button type="button" class="btn sm${i === S.page ? ' primary' : ''}" data-page="${i}">Pagina ${i + 1}</button>`).join('') +
    (S.total > S.pages.length ? `<span>van ${S.total}; de eerste ${S.pages.length} zijn gelezen</span>` : '');
}
function placeLabels() {     // a label goes where it covers no other field: above, right, left or below; else it shows on hover or focus
  const boxes = $$('.box', els.sheet), taken = boxes.map(b => b.getBoundingClientRect()), edge = els.sheet.getBoundingClientRect();
  const hit = (a, b) => a.left < b.right - 1 && a.right > b.left + 1 && a.top < b.bottom - 1 && a.bottom > b.top + 1;
  boxes.forEach((b, i) => {
    for (const pos of ['t', 'r', 'l', 'b', 'n']) {
      b.dataset.pos = pos;
      if (pos === 'n') break;
      const r = b.firstElementChild.getBoundingClientRect();
      if (r.left < edge.left || r.right > edge.right || r.top < edge.top || r.bottom > edge.bottom) continue;
      if (taken.some((t, j) => j !== i && hit(r, t))) continue;
      taken.push(r); break;
    }
  });
}
function mark(key, on) {
  const b = S && S.boxes[key];
  if (on && b && b[0] !== S.page) { S.page = b[0]; drawSheet(); }
  $$('.box', els.sheet).forEach(x => x.classList.toggle('on', on && x.dataset.box === key));
  if (on) { const el = $(`.box[data-box="${key}"]`, els.sheet); if (el && el.scrollIntoView) el.scrollIntoView({block: 'nearest', inline: 'nearest'}); }
}
function checkHtml() {
  const f = S.f, net = f.vat.reduce((s, v) => s + cents(v.base), 0), tax = f.vat.reduce((s, v) => s + cents(v.amount), 0);
  if (!f.vat.length || f.gross == null || f.gross === '') return '<div class="sb-check na"><b><i data-s="?"></i>Subtotaal + btw = totaal</b><code>Nog geen bedragen. Vul een grondslag, de btw en het totaal in: de controle rekent direct mee.</code></div>';
  const gross = cents(f.gross), ok = net + tax === gross;
  const per = f.vat.map(v => { const want = Math.round(cents(v.base) * v.rate / 100), good = Math.abs(want - cents(v.amount)) <= 1;
    return `<li class="${good ? '' : 'bad'}">${String(v.rate).replace('.', ',')}% van ${nlc(cents(v.base))} = ${nlc(want)}${good ? '' : `, op het stuk ${nlc(cents(v.amount))}`}</li>`; });
  const allGood = ok && !per.some(p => p.includes('class="bad"'));
  return `<div class="sb-check ${allGood ? '' : 'bad'}"><b><i data-s="${allGood ? '✓' : '!'}"></i>Subtotaal + btw = totaal${allGood ? ': sluit op de cent' : ': sluit niet'}</b>
    <code>${nlc(net)} + ${nlc(tax)} = ${nlc(net + tax)}${ok ? '' : ` · op het stuk staat ${nlc(gross)} · verschil € ${nlc(Math.abs(gross - net - tax))}`}</code><ul>${per.join('')}</ul></div>`;
}
function glHtml() {
  const g = suggest(S);
  return `<div class="sb-gl"><h3>Voorstel grootboekrekening</h3><div class="acc"><code>${esc(g.acc[0])}</code>${esc(g.acc[1])}<span class="rgs" title="Referentiegrootboekschema">RGS ${esc(g.acc[2])}</span></div>
    <p>${esc(g.why)}</p>${g.notes.length ? `<ul>${g.notes.map(n => `<li class="${n[0]}">${esc(n[1])}</li>`).join('')}</ul>` : ''}</div>`;
}
function totalsHtml() {
  const net = S.f.vat.reduce((s, v) => s + cents(v.base), 0), tax = S.f.vat.reduce((s, v) => s + cents(v.amount), 0);
  return `<td>Subtotaal</td><td class="num">${nlc(net)}</td><td class="num">${nlc(tax)}</td>`;
}
function ublHtml() {
  const xml = buildUbl(S, false), rules = verify(xml), good = rules.filter(r => r.ok).length;
  return `<div class="sb-ubl-h"><span>SI-UBL 2.0 (NLCIUS) · concept · ${good} van ${rules.length} regels in orde</span><span><button type="button" class="btn sm" data-act="copy">Kopieer</button> <button type="button" class="btn sm primary" data-act="download">Download .xml</button></span></div>
    <ul class="rules">${rules.map(c => `<li class="${c.ok ? '' : 'bad'}"><i>${c.ok ? '✓' : '✕'}</i><code>${esc(c.rule)}</code><span>${esc(c.text)}<small>${esc(c.detail)}</small></span></li>`).join('')}</ul>
    <pre class="code">${hl(xml)}</pre>`;
}
function render() {
  const f = S.f;
  els.name.textContent = S.name;
  els.meta.textContent = [S.pages.length > 1 ? `${S.total || S.pages.length} pagina's` : '', S.found ? `${S.found} velden gevonden` : '', S.ms != null ? `gelezen in ${Math.max(1, Math.round(S.ms))} ms` : ''].filter(Boolean).join(' · ');
  drawSheet();
  if (!f.vat.length) f.vat = [{rate: 21, base: 0, amount: 0, empty: true}];
  const rows = f.vat;
  els.fields.innerHTML = `<p class="sb-note${S.warn ? ' warn' : ''}">${esc(S.note)}</p>
    <div class="sb-g">${FIELDS.map(([k, label, c]) => `<label for="sf-${k}"><i style="--c:var(--f-${c})"></i>${label}</label><input id="sf-${k}" data-k="${k}" style="--c:var(--f-${c})" type="text" autocomplete="off" spellcheck="false"${k === 'date' || k === 'due' ? ' placeholder="dd-mm-jjjj" inputmode="numeric"' : ''} value="${esc(k === 'iban' ? group4(f[k]) : k === 'date' || k === 'due' ? dmy(f[k]) : f[k] || '')}">`).join('')}</div>
    <table class="sb-vat"><tr><th>Tarief</th><th class="num">Grondslag</th><th class="num">Btw</th></tr>
      ${rows.map((v, i) => `<tr><td><i style="--c:var(--f-${RATE_CLS(v.rate)})"></i>${String(v.rate).replace('.', ',')}%${S.reverse && !cents(v.amount) && v.rate === 0 ? ' verlegd' : ''}</td>
        <td><input type="text" inputmode="decimal" aria-label="Grondslag ${v.rate}%" data-v="${i}" data-p="base" data-box="b${v.rate}" style="--c:var(--f-${RATE_CLS(v.rate)})" value="${v.empty ? '' : nl(v.base)}"></td>
        <td><input type="text" inputmode="decimal" aria-label="Btw ${v.rate}%" data-v="${i}" data-p="amount" data-box="a${v.rate}" style="--c:var(--f-${RATE_CLS(v.rate)})" value="${v.empty ? '' : nl(v.amount)}"></td></tr>`).join('')}
      <tr class="tot" id="sb-tot">${totalsHtml()}</tr>
      <tr><td><i style="--c:var(--f-gross)"></i>Totaal</td><td colspan="2"><input type="text" inputmode="decimal" aria-label="Totaal op het stuk" data-k="gross" data-box="gross" style="--c:var(--f-gross)" value="${nl(f.gross)}"></td></tr>
    </table>
    <div id="sb-check">${checkHtml()}</div><div id="sb-gl">${glHtml()}</div>`;
  els.ubl.innerHTML = ublHtml();
}
function refresh() {
  $('#sb-tot').innerHTML = totalsHtml(); $('#sb-check').innerHTML = checkHtml(); $('#sb-gl').innerHTML = glHtml();
  els.ubl.innerHTML = ublHtml();
}
els.fields.addEventListener('input', e => {
  const t = e.target;
  if (!S || !t.matches('input')) return;
  if (t.dataset.v != null) { const v = S.f.vat[+t.dataset.v]; v[t.dataset.p] = num(t.value) || 0; delete v.empty; delete v.derived; }
  else if (t.dataset.k === 'gross') S.f.gross = t.value.trim() ? num(t.value) : null;
  else if (t.dataset.k === 'iban') S.f.iban = t.value.replace(/\s/g, '').toUpperCase();
  else if (t.dataset.k === 'date' || t.dataset.k === 'due') S.f[t.dataset.k] = iso(t.value.trim());
  else S.f[t.dataset.k] = t.value.trim();
  refresh();
});
els.fields.addEventListener('focusin', e => { const t = e.target; if (S && t.matches('input')) { $$('input', els.fields).forEach(i => i.classList.toggle('on', i === t)); mark(t.dataset.box || t.dataset.k, true); } });
els.fields.addEventListener('focusout', e => { if (e.target.matches('input')) e.target.classList.remove('on'); });
els.sheet.addEventListener('click', e => {
  const b = e.target.closest('.box'); if (!b) return;
  setTab('fields');
  const inp = $(`[data-box="${b.dataset.box}"]`, els.fields) || $(`[data-k="${b.dataset.box}"]`, els.fields);
  if (inp) inp.focus({preventScroll: matchMedia('(min-width: 761px)').matches});
});
els.pager.addEventListener('click', e => { const b = e.target.closest('[data-page]'); if (b && S) { S.page = +b.dataset.page; drawSheet(); } });
$$('.sb-tabs button').forEach(b => b.addEventListener('click', () => setTab(b.dataset.t)));
addEventListener('resize', () => { if (S && !els.sheet.hidden) placeLabels(); });
els.ubl.addEventListener('click', async e => {
  const b = e.target.closest('[data-act]'); if (!b || !S) return;
  const xml = buildUbl(S, true);
  if (b.dataset.act === 'copy') {
    try { await navigator.clipboard.writeText(buildUbl(S, false)); b.textContent = 'Gekopieerd'; } catch (err) { b.textContent = 'Kopiëren lukt hier niet'; }
    setTimeout(() => { b.textContent = 'Kopieer'; }, 1600);
  } else {
    const a = document.createElement('a'), safe = (S.f.number || 'concept').replace(/[^\w.-]+/g, '_');
    a.href = URL.createObjectURL(new Blob([xml], {type: 'application/xml'})); a.download = `si-ubl-concept-${safe}.xml`;
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }
});

function busy(name) {
  els.name.textContent = name; els.meta.textContent = 'bezig met lezen';
  els.sheet.classList.add('busy');
  els.fields.innerHTML = '<p class="sb-hint">Het document wordt gelezen.</p>'; els.ubl.innerHTML = '';
}
function fail(name, msg) {
  S = null; els.sheet.classList.remove('busy'); els.sheet.hidden = true; els.empty.hidden = false; els.pager.hidden = true;
  els.name.textContent = name; els.meta.textContent = '';
  els.fields.innerHTML = `<p class="sb-note warn">${esc(msg)}</p>`; els.ubl.innerHTML = '';
}
const blank = () => ({supplier: '', number: '', date: '', due: '', vat_id: '', kvk: '', iban: '', customer: '', vat: [], gross: null});
const NO_READ = 'Een foto of scan lezen we in deze browsertest niet uit: dat doet de leesmotor van Scanstraat in de omgeving van uw kantoor. Vul hieronder een paar bedragen in om de controle en het SI-UBL-bestand te zien werken, of kies een van de voorbeelden.';
async function showPdf(bytes, name, own) {
  const me = ++seq; busy(name);
  try {
    const keep = bytes.slice(0), t0 = performance.now();
    const {pages, total} = await openPdf(bytes);
    if (me !== seq) return;
    const hasText = pages.some(p => p.items.length > 3), r = hasText ? parse(pages) : {f: blank(), boxes: {}, text: '', found: 0};
    S = {name, pages, total, page: 0, pdf: keep, f: r.f, boxes: r.boxes, text: r.text, reverse: r.f.reverse, found: r.found, ms: hasText ? performance.now() - t0 : null,
         warn: !hasText || r.found < 3,
         note: !hasText ? 'Deze pdf heeft geen tekstlaag: het is een scan. ' + NO_READ
           : r.found < 3 ? 'Deze pdf is gelezen, maar de vaste regels van deze test vonden weinig velden. Vul aan wat ontbreekt: de controle en het SI-UBL-bestand rekenen mee.'
           : `Live gelezen ${own ? 'uit uw pdf, in uw browser' : 'uit de pdf'}, met vaste regels en zonder AI. Klopt een veld niet? Verbeter het: de controle en het SI-UBL-bestand rekenen mee.`};
    if (!S.f.customer && !own) S.f.customer = '';
    els.sheet.classList.remove('busy'); render();
  } catch (err) {
    if (me === seq) fail(name, /password/i.test(String(err && err.name) + String(err && err.message)) ? 'Deze pdf is met een wachtwoord beveiligd en kan hier niet worden geopend.' : 'Dit bestand kon niet als pdf worden geopend. Probeer een ander bestand of een van de voorbeelden.');
  }
}
function showImage(src, name, sample) {
  const me = ++seq; busy(name);
  const img = new Image();
  img.alt = sample ? 'Foto van een kassabon (fictief)' : 'Uw afbeelding';
  img.onload = () => {
    if (me !== seq) return;
    S = sample ? {name, pages: [{el: img}], total: 1, page: 0, f: JSON.parse(JSON.stringify(sample.f)), boxes: sample.boxes, text: '', gl: sample.gl, found: Object.keys(sample.boxes).length, note: sample.note}
               : {name, pages: [{el: img}], total: 1, page: 0, f: blank(), boxes: {}, text: '', found: 0, warn: true, note: NO_READ};
    setTimeout(() => { if (me === seq) { els.sheet.classList.remove('busy'); render(); } }, sample ? 420 : 0);
    els.empty.hidden = true; els.sheet.hidden = false; els.sheet.replaceChildren(img);
  };
  img.onerror = () => { if (me === seq) fail(name, 'Deze afbeelding kon niet worden geopend.'); };
  img.src = src;
}
async function pick(key) {
  const s = SAMPLES[key]; if (!s) return;
  $$('.sb-sample').forEach(b => b.classList.toggle('on', b.dataset.sample === key));
  setTab('fields');
  if (s.image) return showImage(BASE + s.image, s.name, s);
  try {
    busy(s.name);
    const res = await fetch(BASE + s.pdf);
    if (!res.ok) throw new Error(res.status);
    await showPdf(new Uint8Array(await res.arrayBuffer()), s.name, false);
  } catch (err) { fail(s.name, 'Het voorbeeld kon niet worden geladen. Controleer uw verbinding en probeer het opnieuw.'); }
}
async function own(file) {
  if (!file) return;
  $$('.sb-sample').forEach(b => b.classList.remove('on'));
  setTab('fields');
  if (file.size > 15 * 1024 * 1024) return fail(file.name, 'Dit bestand is groter dan 15 MB. Kies een kleiner bestand.');
  const pdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name);
  if (pdf) return showPdf(new Uint8Array(await file.arrayBuffer()), file.name, true);
  if (file.type.startsWith('image/')) return showImage(URL.createObjectURL(file), file.name, null);
  fail(file.name, 'Dit bestandstype kan hier niet worden getoond. Kies een pdf of een foto.');
}
$$('.sb-sample').forEach(b => b.addEventListener('click', () => pick(b.dataset.sample)));
els.file.addEventListener('change', () => { own(els.file.files[0]); els.file.value = ''; });
['dragenter', 'dragover'].forEach(ev => root.addEventListener(ev, e => { e.preventDefault(); els.drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => root.addEventListener(ev, e => { e.preventDefault(); els.drop.classList.remove('over'); }));
root.addEventListener('drop', e => { if (e.dataTransfer && e.dataTransfer.files.length) own(e.dataTransfer.files[0]); });

// the first example opens by itself when the visitor gets near, so the section never sits empty
if ('IntersectionObserver' in window) {
  const io = new IntersectionObserver(es => { if (es.some(x => x.isIntersecting)) { io.disconnect(); if (!S && !seq) pick('groothandel'); } }, {rootMargin: '300px'});
  io.observe(root);
} else pick('groothandel');
})();
