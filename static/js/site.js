/* Scanstraat homepage: the three scenarios in the hero and the calculator.
   Everything runs on the fixed sample that the tour uses (embedded in the page); nothing is sent anywhere. */
(() => {
'use strict';
const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => [...r.querySelectorAll(s)];
const src = $('#hero-data');
const D = src ? JSON.parse(src.textContent) : null;
const calm = matchMedia('(prefers-reduced-motion: reduce)').matches;
const nl = (v, d = 0) => Number(v).toLocaleString('nl-NL', {minimumFractionDigits: d, maximumFractionDigits: d});
const eur = (v, d = 0) => '€ ' + nl(v, d);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const hhmm = () => new Date().toLocaleTimeString('nl-NL', {hour: '2-digit', minute: '2-digit'});
const wait = ms => new Promise(r => setTimeout(r, calm ? 0 : ms));
const MONTHS = ['jan', 'feb', 'mrt', 'apr', 'mei', 'jun', 'jul', 'aug', 'sep', 'okt', 'nov', 'dec'];
const day = iso => { const m = /^(\d{4})-(\d\d)-(\d\d)/.exec(iso || ''); return m ? `${+m[3]} ${MONTHS[+m[2] - 1]}` : ''; };
const log = (list, html) => { const li = document.createElement('li'); li.innerHTML = `<time>${hhmm()}</time><span>${html}</span>`; list.append(li); };
const nudge = el => { el.classList.remove('shake'); void el.offsetWidth; el.classList.add('shake'); };

/* ---------------------------------------------------------------- hero cockpit */
const hc = $('#hc');
if (hc && D) {
  const tabs = $$('.hc-tabs button', hc), panes = $$('.hc-pane', hc), deep = $('#hc-deep');
  const NEXT = {a: '/stapel', b: '/werkbank', c: '/werkbank'};
  const HASH = {a: '', b: D.fraud ? '#doc=' + D.fraud.doc.id : '', c: D.chase ? '#bon=' + D.chase.line.id : ''};
  let cur = 'a', inView = false;

  /* A: the raw scan falls apart into dossiers and documents */
  const A = (() => {
    const strip = $('#hc-strip'), pgs = $$('.hc-pg', strip), lanes = $$('.hc-lane', hc), state = $('#hc-a-state'), cover = $('#hc-a-cover');
    const nDocs = $('#hc-a-docs'), nBlank = $('#hc-a-blank');
    const idx = {}; D.groups.forEach((g, i) => { idx[g.nr] = i; });
    const docOf = {}; D.docs.forEach(d => d.pages.forEach(p => { docOf[p] = d; }));
    let run = 0, played = false;
    const bump = (lane, sel) => { const s = sel && $(sel, lane); if (s) s.textContent = +s.textContent + 1; };
    function reset() {
      pgs.forEach(p => { p.className = 'hc-pg wait'; delete p.dataset.nr; });
      lanes.forEach(l => { l.classList.remove('in', 'sel'); $$('[data-docs],[data-pages],[data-g],[data-a],[data-r]', l).forEach(s => { s.textContent = '0'; }); });
      strip.classList.remove('pick');
      nDocs.textContent = nBlank.textContent = '0';
    }
    function sort(p, c) {       // what the splitter decides for one page
      const pg = pgs[p.n - 1];
      pg.classList.remove('wait', 'now');
      if (p.kind === 'blank') { pg.classList.add('is-blank'); nBlank.textContent = ++c.blank; return; }
      if (p.kind === 'cover') {
        const i = idx[p.ref], g = D.groups[i];
        if (!g) return;
        pg.classList.add('is-cover', 'd' + i); pg.dataset.nr = g.nr;
        cover.textContent = `SCHEIDINGSBLAD - DOSSIER ${g.nr} (${g.name})`; cover.className = 'd' + i;
        lanes[i].classList.add('in');
        return;
      }
      const d = docOf[p.n], i = d ? idx[d.client_nr] : undefined;
      if (i === undefined) return;
      pg.classList.add('sorted', 'd' + i); pg.dataset.nr = d.client_nr;
      bump(lanes[i], '[data-pages]');
      if (d.pages[d.pages.length - 1] === p.n) {
        bump(lanes[i], '[data-docs]'); bump(lanes[i], {green: '[data-g]', amber: '[data-a]', red: '[data-r]'}[d.light]);
        nDocs.textContent = ++c.docs;
      }
    }
    function done() { state.textContent = 'Gesplitst'; state.classList.remove('busy'); played = true; }
    function finish() { run++; reset(); const c = {blank: 0, docs: 0}; D.pages.forEach(p => sort(p, c)); done(); }
    async function play() {
      if (calm) return finish();
      const me = ++run, c = {blank: 0, docs: 0};
      reset(); played = true;
      state.textContent = 'Scannen'; state.classList.add('busy');
      cover.textContent = `Ruwe scan: ${D.pages.length} pagina's, ongesorteerd`; cover.className = '';
      await wait(420);
      for (const p of D.pages) {
        if (me !== run) return;
        const pg = pgs[p.n - 1];
        pg.classList.remove('wait'); pg.classList.add('now');
        await wait(p.kind === 'cover' ? 330 : 64);
        if (me !== run) return;
        sort(p, c);
      }
      done();
    }
    function pick(nr, on) {
      strip.classList.toggle('pick', on);
      pgs.forEach(p => p.classList.toggle('hl', on && p.dataset.nr === nr));
      lanes.forEach(l => l.classList.toggle('sel', on && l.dataset.nr === nr));
    }
    lanes.forEach(l => {
      l.addEventListener('mouseenter', () => { if (!state.classList.contains('busy')) pick(l.dataset.nr, true); });
      l.addEventListener('mouseleave', () => pick(l.dataset.nr, false));
      l.addEventListener('focus', () => { if (!state.classList.contains('busy')) pick(l.dataset.nr, true); });
      l.addEventListener('blur', () => pick(l.dataset.nr, false));
      l.addEventListener('click', () => { location.href = hc.dataset.tour + '/stapel'; });
    });
    $('#hc-a-replay').addEventListener('click', play);
    finish(); played = false;            // a sorted pile until the visitor reaches it
    return {enter() { if (!played) play(); }, play};
  })();

  /* B: an invoice with a bank account this supplier never used before */
  const B = (() => {
    const pane = $('[data-pane=b]', hc);
    if (!pane || !D.fraud) return {enter() {}, key() {}};
    const view = $('#hc-b-view'), sheet = $('#hc-b-sheet'), img = $('img', sheet), box = $('#hc-b-box'), head = $('#hc-b-head'), flag = $('#hc-b-flag');
    const trail = $('#hc-b-trail'), msg = $('#hc-b-msg'), ok = $('#hc-b-ok'), call = $('#hc-b-call'), no = $('#hc-b-no'), again = $('#hc-b-reset');
    const fb = D.fraud.doc.boxes.iban.box, title = $('b', flag), why = $('p', flag), text0 = [title.textContent, why.textContent];
    let st = '', seen = false;
    function zoom() {            // bring the IBAN on the page into view, large enough to read
      const W = view.clientWidth, H = view.clientHeight, w = sheet.offsetWidth, h = sheet.offsetHeight;
      if (!W || !h) return;
      const s = Math.max(1, Math.min(3.4, 0.7 / (fb[2] - fb[0]), 3.4 * W / 270));
      const cx = (fb[0] + fb[2]) / 2 * w * s, cy = (fb[1] + fb[3]) / 2 * h * s;
      const tx = Math.min(0, Math.max(W - w * s, W / 2 - cx)), ty = Math.min(0, Math.max(H - h * s, H * .46 - cy));
      sheet.style.setProperty('--s', s);
      sheet.style.transform = `translate(${tx.toFixed(1)}px,${ty.toFixed(1)}px) scale(${s.toFixed(3)})`;
    }
    const say = (t, good) => { msg.textContent = t; msg.classList.toggle('ok', !!good); };
    function reset() {
      st = 'open'; trail.replaceChildren(); say('');
      flag.classList.remove('done'); title.textContent = text0[0]; why.textContent = text0[1];
      head.className = 'hc-h l-red'; box.classList.add('bad');
      ok.disabled = call.disabled = no.disabled = false; ok.classList.remove('armed'); again.hidden = true;
      log(trail, `<b>Rode vlag</b>: IBAN wijkt af van ${D.fraud.earlier} eerdere boekingen`);
    }
    function approve() {
      if (st === 'open') { say('Geblokkeerd. Zolang de rode vlag openstaat, kan dit stuk niet worden goedgekeurd.'); nudge(ok); nudge(flag); return; }
      if (st !== 'checked') return;
      st = 'approved'; head.className = 'hc-h l-green';
      log(trail, '<b>Goedgekeurd</b> door u, met de notitie bij de vlag');
      say('Goedgekeurd. Het stuk staat klaar voor de export als SI-UBL 2.0.', true);
      ok.disabled = no.disabled = true; ok.classList.remove('armed'); again.hidden = false;
    }
    ok.addEventListener('click', approve);
    call.addEventListener('click', () => {
      if (st !== 'open') return;
      st = 'checked'; flag.classList.add('done'); box.classList.remove('bad'); head.className = 'hc-h l-amber';
      title.textContent = 'Nieuw IBAN bevestigd met een terugbelcontrole';
      why.textContent = 'Notitie bij de vlag: teruggebeld op het bekende nummer, nieuw rekeningnummer bevestigd.';
      log(trail, '<b>Terugbelcontrole vastgelegd</b> door u');
      say('De vlag heeft een notitie. Goedkeuren kan nu.', true);
      call.disabled = true; ok.classList.add('armed'); ok.focus({preventScroll: true});
    });
    no.addEventListener('click', () => {
      if (st !== 'open' && st !== 'checked') return;
      st = 'rejected'; head.className = 'hc-h l-red';
      log(trail, '<b>Afgekeurd</b> door u: IBAN niet bevestigd');
      say('Afgekeurd. Het stuk gaat met de reden terug en komt niet in de export.');
      ok.disabled = call.disabled = no.disabled = true; ok.classList.remove('armed'); again.hidden = false;
    });
    again.addEventListener('click', reset);
    addEventListener('resize', () => { if (cur === 'b') zoom(); });
    img.addEventListener('load', () => { if (cur === 'b') zoom(); });
    return {
      enter() {
        if (!seen) { seen = true; reset(); }
        const go = () => setTimeout(zoom, calm ? 0 : 450);
        if (img.complete && img.naturalWidth) go(); else if (img.decode) img.decode().then(go, go); else go();
      },
      key(e) { if (e.key === 'Enter') { approve(); return true; } }
    };
  })();

  /* C: a card payment without a receipt, asked for in one message */
  const C = (() => {
    const pane = $('[data-pane=c]', hc);
    if (!pane || !D.chase) return {enter() {}, key() {}};
    const card = $('#hc-c-card'), st = $('#hc-c-st'), concept = $('#hc-c-concept'), trail = $('#hc-c-trail'), chat = $('#hc-c-chat');
    const send = $('#hc-c-send'), again = $('#hc-c-reset'), L = D.chase.line, R = D.chase.reply, doc = R.doc.result;
    const first = chat.innerHTML, who = (D.chase.client && D.chase.client.contact || 'De klant').split(' ')[0];
    let run = 0, busy = false;
    const put = (cls, html) => { const b = document.createElement('div'); b.className = cls; b.innerHTML = html; chat.append(b); chat.scrollTop = chat.scrollHeight; return b; };
    function reset() {
      run++; busy = false; chat.innerHTML = first; trail.replaceChildren();
      card.className = 'bank-card s-ontbreekt'; st.innerHTML = '<i></i><span>Bon ontbreekt bij deze betaling</span>';
      concept.hidden = false; send.disabled = false; send.hidden = false; again.hidden = true;
    }
    async function go() {
      if (busy) return;
      const me = ++run; busy = true; send.disabled = true;
      card.className = 'bank-card s-gevraagd'; st.innerHTML = '<i></i><span>Gevraagd via WhatsApp</span>';
      concept.hidden = true;
      log(trail, '<b>Bericht goedgekeurd</b> door u en verstuurd');
      const hint = $('#hc-c-wait', chat); if (hint) hint.remove();
      put('bub', `<p>${esc(L.text)}</p><small>${hhmm()}</small>`);
      await wait(1500); if (me !== run) return;
      const note = put('ph-note', `${esc(who)} maakt een foto van de bon`);
      await wait(1700); if (me !== run) return;
      note.remove();
      put('bub me img', `<img src="${esc(R.image)}" alt="Foto van de kassabon" width="${R.size[0]}" height="${R.size[1]}"><small>${hhmm()} <i>✓✓</i></small>`);
      log(trail, `<b>Foto ontvangen</b> van ${esc(who)}`);
      await wait(1300); if (me !== run) return;
      const vat = (doc.vat_lines || [])[0];
      card.className = 'bank-card s-gekoppeld';
      st.innerHTML = `<i></i><span>Bon gelezen en gekoppeld</span><small>${esc(doc.counterparty)} · bon ${esc(doc.reference)} · ${eur(doc.gross_total, 2)}${vat ? ` · btw ${vat.rate}% ${eur(vat.amount, 2)}` : ''}</small>`;
      log(trail, `<b>Gekoppeld aan de pinbetaling</b> van ${day(L.date)}: bedrag en datum kloppen`);
      log(trail, 'Staat groen in de werkbank, klaar voor uw <kbd>↵</kbd>');
      send.hidden = true; again.hidden = false; busy = false;
    }
    send.addEventListener('click', go);
    again.addEventListener('click', reset);
    return {enter() {}, key(e) { if (e.key.toLowerCase() === 'w' && !send.hidden && !send.disabled) { go(); return true; } }};
  })();

  const S = {a: A, b: B, c: C};
  function show(sc, focus) {
    if (!$(`[data-pane="${sc}"]`, hc)) return;
    cur = sc;
    tabs.forEach(t => { const on = t.dataset.sc === sc; t.setAttribute('aria-selected', on); t.tabIndex = on ? 0 : -1; if (on && focus) t.focus({preventScroll: true}); });
    panes.forEach(p => { p.hidden = p.dataset.pane !== sc; });
    deep.href = hc.dataset.tour + NEXT[sc] + HASH[sc];
    S[sc].enter();
  }
  tabs.forEach((t, i) => {
    t.addEventListener('click', () => show(t.dataset.sc));
    t.addEventListener('keydown', e => {
      const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
      if (step) { e.preventDefault(); show(tabs[(i + step + tabs.length) % tabs.length].dataset.sc, true); }
    });
  });
  // the keys on the buttons work while the window is on screen and no control has the focus
  document.addEventListener('keydown', e => {
    if (!inView || e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented) return;
    if (e.target.closest && e.target.closest('input,textarea,select,button,a,summary,label,[contenteditable]')) return;
    const n = {'1': 'a', '2': 'b', '3': 'c'}[e.key];
    if (n) { show(n); e.preventDefault(); return; }
    if (S[cur].key && S[cur].key(e)) e.preventDefault();
  });
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(es => { inView = es[0].intersectionRatio >= .45; if (inView && cur === 'a') A.enter(); }, {threshold: [0, .45, 1]}).observe(hc);
  } else { inView = true; A.enter(); }
  deep.href = hc.dataset.tour + NEXT.a;
}

/* ---------------------------------------------------------------- calculator */
const calc = $('#calc');
if (calc) {
  const sv = (+calc.dataset.saving || 40) / 100;
  const T = [{max: 100, name: 'Starter', sub: 'tot 100 dossiers', price: +calc.dataset.starter || 295},
             {max: 300, name: 'Kantoor', sub: 'tot 300 dossiers', price: 595},
             {max: Infinity, name: 'Kantoor Plus', sub: 'meer dan 300 dossiers', price: 995, from: true}];
  const d = $('#rc-d'), h = $('#rc-h'), a = $('#rc-a'), i = $('#rc-i'), box = $('#rc-net-box'), fine = $('#rc-fine'), fine0 = fine.textContent, how = $('#rc-how');
  const set = (id, t) => { $(id).textContent = t; };
  const fill = r => r.style.setProperty('--p', ((r.value - r.min) / (r.max - r.min) * 100).toFixed(2) + '%');
  function upd() {
    const n = +d.value, hrs = +h.value, ra = Math.max(0, +a.value || 0), ri = Math.max(0, +i.value || 0);
    const now = n * hrs, saved = now * sv, t = T.find(x => n <= x.max);
    fill(d); fill(h);
    set('#rc-d-o', nl(n)); set('#rc-h-o', nl(hrs, hrs * 100 % 50 ? 2 : 1) + ' uur');
    set('#rc-hours', nl(Math.round(saved)) + ' uur'); set('#rc-fte', nl(saved * 12 / 1600, 2) + ' fte op jaarbasis');
    set('#rc-now', nl(Math.round(now)) + ' uur per maand'); set('#rc-q', nl(Math.round(saved * 3)) + ' uur');
    set('#rc-va', eur(Math.round(saved * ra)) + ' per maand'); set('#rc-vi', eur(Math.round(saved * ri)) + ' per maand');
    set('#rc-tier', t.name); set('#rc-tier-s', t.sub); set('#rc-price', (t.from ? 'vanaf ' : '') + eur(t.price) + ' per maand');
    const net = Math.round(saved * ra) - t.price;
    box.classList.toggle('neg', net < 0); set('#rc-net', (net < 0 ? '− ' : '') + eur(Math.abs(net)));
    how.innerHTML = `<span><b>${nl(n)}</b> dossiers × <b>${nl(hrs, hrs * 100 % 50 ? 2 : 1)}</b> uur = <b>${nl(Math.round(now))}</b> uur per maand</span><span><b>${nl(Math.round(now))}</b> uur × <b>${Math.round(sv * 100)}%</b> = <b>${nl(Math.round(saved))}</b> uur vrijgespeeld</span>`;
    fine.textContent = net < 0 ? 'Bij dit volume verdient het abonnement zich tegen loonkosten niet terug. Dat zeggen we liever nu dan na de proef.'
      : t.from ? 'Rekenvoorbeeld, geen toezegging. Kantoor Plus is prijs op maat; hier gerekend met het vanaf-bedrag.' : fine0;
  }
  [d, h, a, i].forEach(x => x.addEventListener('input', upd));
  upd();
}
})();
