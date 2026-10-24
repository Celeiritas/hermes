// Hermes · Delivery (docs/design.md §6): what came in and went out, and what waits on you (set aside by Janus).
// A list first: the status line, the send box, recent deliveries with filters, what is set aside (quarantined, held,
// undelivered) with its reasons and copyable lines, then the fetchers. Every state is an icon plus a word.
// Nothing here decides: Send runs `hermes message` and Janus judges it; there is no Release button yet (issue #27).
// The road picture (parcels, gate, shelf, cage) waits for the owner's art approval (argus-ui docs/ui-plan.md §13).
leafy.register('delivery', (() => {
  const OUT = {delivered: ['✓', 'delivered'], sent: ['✓', 'sent'], released: ['⇥', 'released'], quarantined: ['⚠', 'quarantined'], quarantine: ['⚠', 'quarantined'],
    held: ['⏸', 'held'], waiting: ['↻', 'waiting'], outbox: ['↻', 'waiting'], undelivered: ['✕', 'undelivered'],
    skipped: ['–', 'skipped'], failed: ['✕', 'failed'], discarded: ['⊘', 'set aside']};
  const BAD = ['quarantined', 'quarantine', 'held', 'undelivered', 'failed'];   // the log says quarantined; the folder is quarantine/
  const FILTERS = [['all', 'All'], ['quarantine', 'Quarantined'], ['outbox', 'Outbox']];
  const ASIDE = [['quarantine', 'Quarantined'], ['held', 'Held'], ['undelivered', 'Undelivered']];
  let A = null, ROOT = null, S = null, filter = 'all', said = '', draft = {route: '', text: ''};
  const open = new Set();                       // set-aside items whose detail is open
  const $ = (s, el = ROOT) => el.querySelector(s), $$ = (s, el = ROOT) => [...el.querySelectorAll(s)];
  const e = s => A.esc(String(s ?? ''));
  const chip = k => { const [i, w] = OUT[k] || ['•', k || 'unknown']; return `<span class="chip o-${e(k)}"><span aria-hidden="true">${i}</span> ${e(w)}</span>`; };
  function size(n) { if (n == null) return ''; const u = ['B', 'KB', 'MB', 'GB']; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return i ? `${n.toFixed(1)} ${u[i]}` : `${n} B`; }
  function ago(iso) {
    const t = Date.parse(iso); if (!t) return '';
    const m = Math.round((Date.now() - t) / 60000);
    if (m < 1) return 'just now'; if (m < 60) return `${m} min ago`; if (m < 24 * 60) return `${Math.round(m / 60)} h ago`;
    return new Date(t).toLocaleDateString([], {weekday: 'short', month: 'short', day: 'numeric'});
  }
  const shown = () => filter === 'quarantine' ? S.recent.filter(r => r.outcome === 'quarantined')
    : filter === 'outbox' ? S.recent.filter(r => r.outcome === 'waiting' || r.outcome === 'undelivered') : S.recent;

  /* ---------- the sections ---------- */
  const row = r => `<li class="drow${BAD.includes(r.outcome) ? ' bad' : ''}">
      <div class="dhead"><span>${chip(r.outcome)} <b>${e(r.name || '(no name)')}</b></span><span class="meta">${e(ago(r.t))}</span></div>
      <p class="meta">${e(r.route || '?')} · ${e(r.from || '?')}${r.to ? ` → ${e(r.to)}` : ''}${r.bytes != null ? ` · ${e(size(r.bytes))}` : ''}</p>
      ${(r.reasons || []).length ? `<p class="why">${(r.reasons || []).map(e).join(' ')}</p>` : ''}</li>`;
  function aside(i) {
    const o = open.has(i.id), lines = (i.folder === 'quarantine' ? [`hermes release ${i.id} --reason checked-by-hand`] : []).concat([`hermes discard ${i.id}`]);
    return `<li class="aside" data-id="${e(i.id)}">
      <div class="dhead"><span>${chip(i.folder)} <b>${e(i.name || '(no name)')}</b></span><span class="meta">${e(ago(i.since))}</span></div>
      <p class="why">${(i.reasons || []).length ? (i.reasons || []).map(e).join(' ') : 'No reason was given.'}</p>
      <div class="acts"><button type="button" class="more" aria-expanded="${o}">${o ? 'Hide the details' : 'Show the details'}</button></div>
      <div class="detail" ${o ? '' : 'hidden'}>
        <dl><dt>From</dt><dd>${e(i.from || 'unknown')}</dd><dt>Route</dt><dd>${e(i.route || '?')}</dd><dt>Size</dt><dd>${e(size(i.bytes) || '?')}</dd>
          <dt>SHA-256</dt><dd><code>${e(i.sha256 || 'not recorded')}</code></dd></dl>
        <p class="meta">Hermes never releases on his own. To act on it, run one of these on the server:</p>
        ${lines.map(l => `<div class="cmd"><code>${e(l)}</code><button type="button" class="copy" data-line="${e(l)}">Copy ${l.split(' ')[1]} line</button></div>`).join('')}
      </div></li>`;
  }
  const fetcher = f => `<li class="frow" data-job="${e(f.job)}">
      <div><b>${e(f.job)}</b><p class="meta">${e(f.what)}${f.last ? ` Last: ${e(f.last.name || '')} ${e(ago(f.last.t))} (${e((OUT[f.last.outcome] || [, f.last.outcome])[1])}).` : ''}</p></div>
      <button type="button" class="runjob">${e(f.label)}</button></li>`;

  function draw() {
    const focus = document.activeElement && ROOT.contains(document.activeElement) ? key(document.activeElement) : null;
    if (!S.ok) {
      ROOT.innerHTML = `<div class="delivery"><p class="dline" id="dstatus" role="status">${e(S.line)}</p><div class="empty"><b>${e(S.problem)}</b> The page shows deliveries once Hermes is installed and its routes.json is valid (<code>hermes check-config</code> explains what is wrong).</div></div>`;
      return;
    }
    const set = ASIDE.flatMap(([f]) => S[f] || []), list = shown();
    if (!draft.route || !S.routes.some(r => r.id === draft.route)) draft.route = (S.routes[0] || {}).id || '';
    ROOT.innerHTML = `<div class="delivery">
      <p class="dline" id="dstatus" role="status">${e(said || S.line)}</p>
      ${S.alert ? `<div class="dalert"><p><span aria-hidden="true">⚠</span> <b>Hermes says:</b> ${e(S.alert.message)}</p><button type="button" class="seen">Mark as read</button></div>` : ''}
      <div class="dcols"><div class="dcol">
      <section aria-labelledby="d-send"><h3 id="d-send">Send a note</h3>
        ${S.routes.length ? `<form class="send">
          <label for="d-route">Route</label>
          <select id="d-route">${S.routes.map(r => `<option value="${e(r.id)}" ${r.id === draft.route ? 'selected' : ''}>${e(r.id)}${r.to ? ` → ${e(r.to)}` : ''}</option>`).join('')}</select>
          <label for="d-text">Note, link or magnet link</label>
          <textarea id="d-text" rows="3" maxlength="4096" spellcheck="false">${e(draft.text)}</textarea>
          <p class="meta">Janus checks it like any file. Files go by Taildrop from a phone, or by Send To on a PC.</p>
          <div class="acts"><button type="submit" class="go">Send note</button></div></form>`
          : '<p class="empty">No open routes in routes.json.</p>'}</section>
      <section aria-labelledby="d-aside"><h3 id="d-aside">Set aside${set.length ? ` <span class="count">${set.length}</span>` : ''}</h3>
        ${set.length ? `<ul class="asides">${set.map(aside).join('')}</ul>` : '<p class="empty">Nothing is set aside. Quarantined and held deliveries wait here with Janus\'s reasons.</p>'}</section>
      </div><div class="dcol">
      <section aria-labelledby="d-recent"><h3 id="d-recent">Recent deliveries</h3>
        <div class="filters" role="group" aria-label="Show">${FILTERS.map(([v, l]) => `<button type="button" data-f="${v}" aria-pressed="${v === filter}">${l}</button>`).join('')}</div>
        ${list.length ? `<ul class="recent">${list.map(row).join('')}</ul>` : '<p class="empty">Nothing here yet.</p>'}</section>
      <section aria-labelledby="d-fetch"><h3 id="d-fetch">Fetchers</h3><ul class="fetchers">${S.fetchers.map(fetcher).join('')}</ul></section>
      </div></div></div>`;
    wire();
    if (focus) { const el = find(focus); if (el) el.focus(); }
  }
  // a redraw must keep keyboard focus: remember the control by its row and its class (or its id), find it again after
  const key = el => el.id ? ['#', el.id] : el.dataset && el.dataset.f ? ['f', el.dataset.f] : (() => { const li = el.closest('[data-id],[data-job]'); return li ? [li.dataset.id || li.dataset.job, el.className.split(' ')[0]] : null; })();
  const find = ([a, b]) => a === '#' ? $('#' + CSS.escape(b)) : a === 'f' ? $(`[data-f="${b}"]`) : $$('[data-id],[data-job]').filter(li => (li.dataset.id || li.dataset.job) === a).map(li => li.querySelector('.' + CSS.escape(b))).find(Boolean);

  /* ---------- what the buttons do: each one asks Hermes and shows what he said ---------- */
  function report(text) {
    said = text; const st = $('#dstatus'); if (st) st.textContent = said;
    A.toast(said.split('\n')[0].slice(0, 200));
    clearTimeout(report.t); report.t = setTimeout(() => { said = ''; const s = ROOT && $('#dstatus'); if (s && S) s.textContent = S.line; }, 30000);
  }
  async function ask(path, body, done) {
    const res = await A.post(path, body), ok = !!(res.ok && res.data && res.data.ok), code = res.data && res.data.code;
    const msg = res.status === 0 ? "Couldn't reach Elaia's server. Nothing was sent." : (res.data && res.data.msg) || '';
    report(ok ? done(msg) : msg || `Hermes said no (${res.status}).`);
    return {ok, msg, code};
  }
  function wire() {
    const form = $('form.send');
    if (form) {
      const sel = $('#d-route'), ta = $('#d-text');
      sel.onchange = () => { draft.route = sel.value; }; ta.oninput = () => { draft.text = ta.value; };
      form.onsubmit = async ev => {
        ev.preventDefault(); const go = $('.go', form); if (!ta.value.trim()) { ta.focus(); return report('Type a note, a link or a magnet link first.'); }
        go.disabled = true; report('Handed to Hermes · Janus checking…');
        const r = await ask('send', {route: sel.value, text: ta.value}, msg => msg || 'Sent.');
        // the verdict decides the reaction: delivered or sent, Argus nods; set aside, he winces (Elaia 4.20, api.argus)
        const bad = !r.ok || /quarantined|held|undelivered|refused/.test(r.msg);
        if (A.argus) A.argus.react(bad ? 'wince' : 'nod');
        // exit 1: Hermes took it and Janus set it aside, so it must not be sent twice; exit 2: refused, the text stays to fix
        if (r.ok || r.code === 1) { draft.text = ''; }
        if (r.ok) { if (A.fx && !bad) A.fx.motes(go, $('#d-recent') || go, {color: '#7FB3D5'}); }
        await load(); const t = $('#d-text'); if (t) t.focus();
      };
    }
    $$('.aside .more').forEach(b => b.onclick = () => { const id = b.closest('.aside').dataset.id; open.has(id) ? open.delete(id) : open.add(id); draw(); });
    $$('.copy').forEach(b => b.onclick = async () => {
      try { await navigator.clipboard.writeText(b.dataset.line); report(`Copied: ${b.dataset.line}`); } catch (err) { report(`Copy it by hand: ${b.dataset.line}`); }
    });
    $$('.runjob').forEach(b => b.onclick = async () => {
      const job = b.closest('.frow').dataset.job; b.disabled = true;
      await ask('run', {job}, () => `Started ${job}. New deliveries show up here when it finishes.`);
      setTimeout(load, 3000);
    });
    $$('[data-f]').forEach(b => b.onclick = () => { filter = b.dataset.f; draw(); });
    const seen = $('.seen'); if (seen) seen.onclick = async () => { await ask('seen', {}, () => 'Marked as read. Argus calms down on his next look.'); load(); };
  }

  /* ---------- reading: with Elaia's 15 s refresh; never under the cursor in the send box ---------- */
  async function load() {
    const res = await A.get('');
    if (!ROOT || !ROOT.isConnected) return;
    if (!res.ok) { ROOT.innerHTML = `<div class="empty"><b>The Delivery page can't read Hermes right now.</b> Elaia's server didn't answer (${res.status || 'no answer'}). It tries again in 15 seconds.</div>`; return; }
    const typing = document.activeElement && document.activeElement.id === 'd-text' && draft.text;
    const fresh = JSON.stringify(res.data), changed = !S || fresh !== JSON.stringify(S);
    S = res.data;
    if (changed && !typing) draw();
  }
  return {
    async show(el, api) { A = api; ROOT = el; S = null; said = ''; await load(); },
    refresh(el, api) { A = api; ROOT = el; load(); },
  };
})());
