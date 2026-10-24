// Hermes · Delivery in a real browser, under Elaia, against a fake hermes (tests/delivery/fake_hermes.py). run.sh copies
// this file into an argus-ui checkout's tests/ and runs it there, so './lib' is argus-ui's test helper.
// hermes#11 "done means": the list renders from a fixture log; the send box posts and shows the verdict; run accepts only
// the allowlisted jobs; a cross-site POST is refused. Plus: set-aside detail with copyable lines, filters, the alert,
// Home's "Needs you", keyboard focus across a refresh, 375 px without sideways scroll, every state an icon and a word.
const {BASE, check, page, noOverflow, shot, run} = require('./lib');
const fs = require('fs'), path = require('path');
const H = path.join(path.dirname(process.env.FIXTURES), 'home');            // Elaia's throwaway HOME (argus-ui tests/run.sh)
const at = (...p) => path.join(H, ...p);
// local time with its offset, as Hermes writes it (it counts "today" by the server's local date)
const pad = n => String(Math.abs(n)).padStart(2, '0');
function iso(m) {
  const d = new Date(Date.now() + m * 60000), o = -d.getTimezoneOffset();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}${o < 0 ? '-' : '+'}${pad(Math.trunc(o / 60))}:${pad(o % 60)}`;
}
const month = iso(0).slice(0, 7);                       // the month file Hermes reads
const calls = () => { try { return fs.readFileSync(at('calls.jsonl'), 'utf8').trim().split('\n').filter(Boolean).map(l => JSON.parse(l)); } catch (e) { return []; } };
const ORIGIN = new URL(BASE).origin;

run(async b => {
  for (const d of ['.config/hermes', '.local/share/hermes/stage', '.local/share/hermes/log', '.local/state/argus-ui/alerts', 'mail', 'watch']) fs.mkdirSync(at(d), {recursive: true});
  fs.writeFileSync(at('.config/hermes/routes.json'), JSON.stringify({version: 1, stages: {home: '~/.local/share/hermes/stage'}, routes: {
    servermail: {to: '~/mail', stage: 'home'}, torrent: {to: '~/watch', stage: 'home', conflict: 'skip'},
    pcmail: {to: 'taildrop:{{PC}}', stage: 'home', retry_hours: 24}, closed: {to: '~/mail', stage: 'home', enabled: false}},
    fetch: {decks: {job: 'hermes-decks', route: 'servermail', repo: 'git@example.com:you/realm.git', paths: ['decks/*.json']}}}));
  const line = (m, o) => JSON.stringify({t: iso(m), dir: 'in', from: '{{PHONE}}', reasons: [], ...o});
  fs.writeFileSync(at('.local/share/hermes/log', `deliveries-${month}.jsonl`), [
    line(-50, {id: 'a1', route: 'torrent', to: '~/watch/show.torrent', name: 'show.torrent', bytes: 41000, outcome: 'delivered'}),
    line(-40, {id: 'a2', route: 'pcmail', dir: 'out', from: 'local', to: 'taildrop:{{PC}}', name: 'report.pdf', bytes: 2.4e6, outcome: 'waiting', reasons: ['{{PC}} is asleep']}),
    line(-30, {id: 'a3', route: 'servermail', name: 'setup.exe', bytes: 2048, outcome: 'quarantined', reasons: ['An executable (.exe) is never delivered.']}),
    line(-20, {id: 'a4', route: 'servermail', to: '~/mail/notes.txt', name: 'notes.txt', bytes: 300, outcome: 'delivered'})].join('\n') + '\n');
  const q = at('.local/share/hermes/stage/quarantine/20261023-101500-q1');
  fs.mkdirSync(q, {recursive: true});
  fs.writeFileSync(path.join(q, 'meta.json'), JSON.stringify({id: '20261023-101500-q1', route: 'servermail', name: 'setup.exe', bytes: 2048, sha256: 'ab'.repeat(32),
    sender: {key: '{{PHONE}}', via: 'ssh'}, received: iso(-30)}));
  fs.writeFileSync(path.join(q, 'verdict.json'), JSON.stringify({verdict: 'quarantine', reasons: ['An executable (.exe) is never delivered.']}));
  fs.writeFileSync(at('.local/state/argus-ui/alerts/hermes.json'), JSON.stringify({job: 'hermes-door', level: 'alert', since: iso(-30),
    message: '1 delivery quarantined: An executable (.exe) is never delivered.', link: 'delivery'}));

  // ---- Home: Needs you points here
  const p = await page(b);
  await p.goto(BASE + 'home'); await p.waitForTimeout(900);
  const need = p.locator('.need', {hasText: 'Delivery'});
  check(await need.count() >= 1 && /Look at the set-aside one/.test(await p.locator('.home').textContent()), 'Home: Needs you says something is set aside, with a verb-first button');

  // ---- the page: status line, alert, sections in order
  await p.goto(BASE + 'delivery'); await p.locator('.delivery').waitFor();
  check(/2 delivered today/.test(await p.locator('#dstatus').textContent()) && /1 quarantined/.test(await p.locator('#dstatus').textContent()), `the status line counts today and what is set aside ("${await p.locator('#dstatus').textContent()}")`);
  check(/Hermes says:.*quarantined/.test(await p.locator('.dalert').textContent()), 'the alert shows what Hermes said');
  const heads = await p.locator('.delivery h3').allTextContents();
  check(heads.map(h => h.replace(/\s*\d+$/, '')).join(' | ') === 'Send a note | Set aside | Recent deliveries | Fetchers', `sections in reading order: ${heads.join(' | ')}`);

  // recent deliveries from the log, newest first, each state an icon and a word
  const names = await p.locator('.recent .drow b').allTextContents();
  check(names.join(',') === 'notes.txt,setup.exe,report.pdf,show.torrent', `recent deliveries, newest first (${names.join(', ')})`);
  check(/servermail · \{\{PHONE\}\} → ~\/mail\/notes.txt · 300 B/.test(await p.locator('.recent .drow').first().textContent()), 'each row: route, from → to, size');
  const chips = await p.locator('.delivery .chip').evaluateAll(cs => cs.map(c => [c.querySelector('[aria-hidden]')?.textContent.trim(), c.textContent.replace(/^\S+\s*/, '').trim()]));
  check(chips.length >= 5 && chips.every(([i, w]) => i && w.length > 2), `every state is an icon and a word (${[...new Set(chips.map(c => c.join(' ')))].join(', ')})`);
  await p.locator('[data-f="quarantine"]').click();
  check(await p.locator('[data-f="quarantine"]').getAttribute('aria-pressed') === 'true' && (await p.locator('.recent .drow b').allTextContents()).join() === 'setup.exe', 'the Quarantined filter shows only the quarantined delivery');
  await p.locator('[data-f="outbox"]').click();
  check((await p.locator('.recent .drow b').allTextContents()).join() === 'report.pdf' && /asleep/.test(await p.locator('.recent').textContent()), 'the Outbox filter shows what waits for a sleeping device, and why');
  await p.locator('[data-f="all"]').click();

  // set aside: the reason in plain words, details on demand, copyable lines, no Release button (issue #27)
  const item = p.locator('.aside').first();
  check(/An executable \(\.exe\) is never delivered\./.test(await item.locator('.why').textContent()), 'set aside: the reason in plain words');
  check(await p.locator('.delivery button', {hasText: /^Release/}).count() === 0, 'there is no Release button (Hermes never releases on his own; #27 adds it through Zeus)');
  const more = item.locator('.more');
  check(await more.getAttribute('aria-expanded') === 'false' && await item.locator('.detail').isHidden(), 'details start closed');
  await more.focus(); await p.keyboard.press('Enter'); await p.waitForTimeout(200);
  check(await p.locator('.aside .more').getAttribute('aria-expanded') === 'true' && await p.evaluate(() => document.activeElement.classList.contains('more')), 'Enter opens the details and keeps focus on the button');
  const det = await p.locator('.aside .detail').textContent();
  check(det.includes('ab'.repeat(32)) && /\{\{PHONE\}\}/.test(det) && /2\.0 KB/.test(det), 'details: sender, size and SHA-256');
  const lines = await p.locator('.aside .cmd code').allTextContents();
  check(lines.join(' | ') === 'hermes release 20261023-101500-q1 --reason checked-by-hand | hermes discard 20261023-101500-q1', `the copyable lines (${lines.join(' | ')})`);
  check(await p.locator('.aside .copy').first().textContent() === 'Copy release line', 'copy buttons are verb-first and say which line');
  check(!/\.local[\\/]share[\\/]hermes/.test(await p.content()), "the arrival's folder on the server never reaches the page");

  // ---- send a note: posts, shows the verdict in place, and Argus reacts
  check((await p.locator('#d-route option').allTextContents()).map(t => t.split(' ')[0]).join() === 'servermail,torrent,pcmail', 'the send box offers the open routes only');
  await p.evaluate(() => { window.moved = []; const a = Element.prototype.animate; Element.prototype.animate = function (k, o) { if (this.id === 'argus') moved.push(1); return a.call(this, k, o); }; });
  await p.locator('#d-route').selectOption('servermail');
  await p.locator('#d-text').fill('buy olive oil');
  await p.locator('.send .go').click();
  await p.locator('#dstatus', {hasText: 'delivered'}).waitFor({timeout: 5000});
  const sent = calls().find(c => c.args[0] === 'message');
  check(sent && sent.args.join(' ') === 'message servermail' && sent.stdin === 'buy olive oil', `Send note runs hermes message on that route with the text on stdin (${JSON.stringify(sent)})`);
  check(/note-.*: delivered/.test(await p.locator('#dstatus').textContent()) && await p.locator('#d-text').inputValue() === '', 'the verdict shows in place and the box is cleared');
  await p.waitForTimeout(400);
  check(/note-/.test(await p.locator('.recent .drow b').first().textContent()), 'the note is at the top of Recent deliveries');
  const reacts = await p.evaluate(() => typeof look === 'object' && typeof look.react === 'function');
  check(!reacts || await p.evaluate(() => moved.length > 0), reacts ? 'Argus nods when it was delivered (Elaia 4.20)' : 'Argus reactions: this Elaia is older than 4.20, skipped');
  await p.locator('#d-text').fill('quarantine-me please'); await p.keyboard.press('Tab'); await p.keyboard.press('Enter');
  await p.locator('#dstatus', {hasText: 'quarantined'}).waitFor({timeout: 5000});
  check(true, 'a quarantined message: the verdict says so (keyboard: Tab to Send note, Enter)');
  await p.locator('.send .go').click(); await p.waitForTimeout(400);
  check(/Type a note/.test(await p.locator('#dstatus').textContent()) && calls().filter(c => c.args[0] === 'message').length === 2, 'an empty box sends nothing and says what to do');

  // ---- fetchers: only the allowlisted jobs, and the server refuses any other
  const jobs = await p.locator('.frow').evaluateAll(rs => rs.map(r => r.dataset.job));
  check(jobs.join() === 'hermes-taildrop,hermes-decks', `fetchers: phone mail and the configured decks job (${jobs.join(', ')})`);
  await p.locator('.frow[data-job="hermes-decks"] .runjob').click();
  await p.locator('#dstatus', {hasText: 'Started hermes-decks'}).waitFor({timeout: 5000});
  check(calls().some(c => c.args.join(' ') === '--user start --no-block hermes-decks.service'), 'Fetch decks now starts that job, without waiting for it');
  const post = (body, headers = {}) => p.evaluate(async ([u, body, headers]) => { const r = await fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json', ...headers}, body: JSON.stringify(body)}); return r.status; }, [BASE + 'api/delivery/run', body, headers]);
  check(await post({job: 'argus-ui'}) === 400 && await post({job: 'hermes-decks; reboot'}) === 400, 'run refuses any job not on the list');
  const cross = await p.request.post(BASE + 'api/delivery/run', {data: {job: 'hermes-decks'}, headers: {Origin: 'https://evil.example.com', 'Sec-Fetch-Site': 'cross-site'}}).then(r => r.status());
  check(cross === 403 && calls().filter(c => c.args.includes('hermes-decks.service')).length === 1, `a cross-site POST is refused (${cross})`);

  // ---- the alert: Mark as read runs hermes ack
  await p.locator('.dalert .seen').click();
  await p.locator('.dalert').waitFor({state: 'detached', timeout: 5000}).catch(() => {});
  check(calls().some(c => c.args[0] === 'ack') && await p.locator('.dalert').count() === 0, 'Mark as read runs hermes ack and the alert goes');
  check(p.errors.length === 0, 'no page errors ' + p.errors.join(' | '));
  await shot(p, 'delivery');

  // ---- focus survives Elaia's refresh; typing is never redrawn away
  await p.locator('[data-f="outbox"]').focus(); await p.evaluate(() => refresh()); await p.waitForTimeout(600);
  check(await p.evaluate(() => document.activeElement?.dataset?.f === 'outbox'), 'a refresh keeps keyboard focus on the filter');
  await p.locator('#d-text').fill('half a thought'); fs.appendFileSync(at('.local/share/hermes/log', `deliveries-${month}.jsonl`), line(0, {id: 'a9', route: 'torrent', name: 'late.torrent', bytes: 10, outcome: 'delivered'}) + '\n');
  await p.evaluate(() => refresh()); await p.waitForTimeout(600);
  check(await p.locator('#d-text').inputValue() === 'half a thought' && await p.evaluate(() => document.activeElement.id === 'd-text'), 'while you type, a refresh does not take the text or the focus');

  // ---- phone, reduced motion
  const ph = await page(b, 375, 760); await ph.emulateMedia({reducedMotion: 'reduce'}); await ph.goto(BASE + 'delivery'); await ph.locator('.delivery').waitFor();
  check(await noOverflow(ph), '375 px: no sideways scrolling');
  const small = await ph.locator('.delivery button:visible, .delivery select, .delivery textarea').evaluateAll(es => es.filter(e => e.getBoundingClientRect().height < 44).map(e => e.textContent.trim() || e.id));
  check(small.length === 0, `phone: every control has a 44 px hit area${small.length ? ' (not: ' + small.join(', ') + ')' : ''}`);
  await shot(ph, 'delivery-phone');
  check(ph.errors.length === 0, 'no page errors ' + ph.errors.join(' | '));
});
