/* /veiligheid: switch between the original text, what the egress gate would let out, and local-only mode, for the
   fictional samples rendered by trust.py; and the live test field, which posts to /api/veiligheid/filter (the real
   fixed rules and gate on the server, nothing stored). */
(() => {
'use strict';
const root = document.getElementById('vg-insp');
const data = JSON.parse((document.getElementById('vg-data') || {}).textContent || '{}');
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const TOKEN = /\[(IBAN|EMAIL|TELEFOON|BSN|GEBOORTEDATUM|PERSOON|ADRES)_\d+\]/g;
const KIND = {IBAN: 'IBAN', EMAIL: 'e-mailadres', TELEFOON: 'telefoonnummer', BSN: 'BSN', GEBOORTEDATUM: 'geboortedatum', PERSOON: 'naam van een persoon', ADRES: 'woonadres'};
const marked = t => esc(t).replace(TOKEN, m => `<mark class="tok">${m}</mark>`);
const gateList = r => r.length ? `<ul class="vg-why">${r.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : '';

function verdict(ok, title, body) {
  return `<div class="vg-v ${ok ? 'ok' : 'bad'}"><b>${title}</b>${body || ''}</div>`;
}

if (root && Object.keys(data).length) {
  let sample = Object.keys(data)[0], mode = 'filtered';
  const text = document.getElementById('vg-text'), side = document.getElementById('vg-side'), head = document.getElementById('vg-doc-h');

  function render() {
    const s = data[sample];
    root.querySelectorAll('[data-sample]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.sample === sample)));
    root.querySelectorAll('[data-mode]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.mode === mode)));
    let html = '';
    if (mode === 'original') {
      head.innerHTML = '<span>Zoals het op de server van uw kantoor staat</span><i class="loc">blijft op de server</i>';
      text.innerHTML = esc(s.original);
      html += verdict(false, 'De poort houdt deze tekst tegen', gateList(s.gate_original));
      html += '<p>Zo zou de tekst nooit naar een taalmodel gaan. Ook als het filter een fout maakt, stopt de poort hem.</p>';
    } else if (mode === 'filtered') {
      if (s.stays_local) {
        head.innerHTML = '<span>Naar het taalmodel</span><i class="loc">niets</i>';
        text.innerHTML = '<span class="vg-none">Dit document gaat niet naar buiten, ook niet gefilterd.</span>';
        const cats = Object.entries(s.special).map(([c, t]) => `${c} (${t.join(', ')})`).join('; ');
        html += verdict(true, 'Blijft volledig lokaal', `<p>Mogelijk bijzondere persoonsgegevens (AVG art. 9): ${esc(cats)}. Het lokale model op de server leest dit stuk.</p>`);
        html += '<p class="vg-n">0 tekens naar een externe dienst.</p>';
      } else {
        head.innerHTML = '<span>Dit is alles wat het taalmodel ziet</span><i class="ext">via TLS naar Claude, EU-regio</i>';
        text.innerHTML = marked(s.filtered);
        html += verdict(true, 'De poort laat dit door', '<p>Geen persoonsgegevens en geen bijzondere gegevens meer gevonden.</p>');
        html += `<p class="vg-n">${s.sent_chars} tekens, geen afbeelding</p><p class="vg-hash" title="SHA-256 van precies deze tekst, zoals in het log van uw kantoor">SHA-256 <code>${esc(s.sha256.slice(0, 16))}…</code></p>`;
        html += '<h4>Vervangen</h4><ul class="vg-rep">' + s.replaced.map(r =>
          `<li><mark class="tok">${esc(r.token)}</mark><span>${esc(KIND[r.kind] || r.kind)}</span><span class="vg-by ${r.by === 'model' ? 'm' : 'r'}">${r.by === 'model' ? 'lokaal model' : 'regel'}</span></li>`).join('') + '</ul>';
        html += '<p>Het antwoord krijgt de echte waarden terug op de server. De koppeling tussen <mark class="tok">[IBAN_1]</mark> en het echte nummer verlaat de server nooit.</p>';
      }
    } else {
      head.innerHTML = '<span>Lokale modus</span><i class="loc">niets naar buiten</i>';
      text.innerHTML = esc(s.original);
      html += verdict(true, 'Geen externe AI', '<p>Het lezen gebeurt door een taalmodel op de server van uw kantoor in Duitsland. Er gaat geen tekst en geen afbeelding naar een andere partij.</p>');
      html += '<p class="vg-n">0 tekens naar een externe dienst.</p><p>Per kantoor in te stellen. Langzamer en minder nauwkeurig dan Claude: dat meten we op uw eigen stukken voordat u kiest.</p>';
    }
    head.insertAdjacentHTML('afterbegin', `<b>${esc(s.title)}</b>`);
    side.innerHTML = html;
  }
  root.addEventListener('click', e => {
    const b = e.target.closest('[data-sample],[data-mode]');
    if (!b) return;
    if (b.dataset.sample) sample = b.dataset.sample; else mode = b.dataset.mode;
    render();
  });
  render();
}

const form = document.getElementById('vg-live-form');
if (form) {
  const out = document.getElementById('vg-live-out');
  const csrf = (document.querySelector('meta[name=csrf]') || {}).content || '';
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = form.querySelector('button');
    btn.disabled = true;
    try {
      const res = await fetch('/api/veiligheid/filter', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF': csrf},
        body: JSON.stringify({text: document.getElementById('vg-live-in').value})});
      if (!res.ok) throw new Error(res.status);
      const r = await res.json();
      let html = verdict(!r.gate_original.length, r.gate_original.length ? 'Origineel: de poort houdt dit tegen' : 'Origineel: niets gevonden', gateList(r.gate_original));
      if (r.stays_local) html += verdict(true, 'Na het filter: blijft volledig lokaal', '<p>Bijzondere gegevens gevonden: dit stuk zou niet naar buiten gaan.</p>');
      else html += verdict(!r.gate_filtered.length, r.gate_filtered.length ? 'Na het filter: nog steeds tegengehouden' : 'Na het filter: de poort laat dit door', gateList(r.gate_filtered))
        + `<pre>${marked(r.filtered)}</pre>`;
      out.innerHTML = html + `<p class="vg-fine">${esc(r.note)}</p>`;
    } catch (err) {
      out.innerHTML = '<p>De controle is nu niet bereikbaar. Ververs de pagina en probeer opnieuw.</p>';
    }
    out.hidden = false;
    btn.disabled = false;
  });
}
})();
