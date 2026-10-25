// Drives the real booking page in jsdom against a running dev server.
// Usage: BASE=http://localhost:3000 node tests/ui_smoke.js   (needs: npm i jsdom, one-off, outside this project)
const { JSDOM } = require('jsdom');
const fs = require('fs'), path = require('path');
const base = process.env.BASE;
const html = fs.readFileSync(path.join(__dirname, '..', 'public', 'book.html'), 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function wait(fn, what, ms = 8000) {
  const t = Date.now();
  while (Date.now() - t < ms) { try { const v = fn(); if (v) return v; } catch (_) {} await sleep(40); }
  throw new Error('timed out waiting for: ' + what);
}
(async () => {
  const dom = new JSDOM(html, { url: base + '/book.html', runScripts: 'dangerously', pretendToBeVisual: true,
    beforeParse(w) { w.fetch = (u, o) => fetch(new URL(u, base), o); w.scrollTo = () => {}; } });
  const d = dom.window.document, main = () => d.getElementById('main').textContent;
  const type = (id, v) => { const e = d.getElementById(id); e.value = v; e.dispatchEvent(new dom.window.Event('input', { bubbles: true })); };

  const cards = await wait(() => d.querySelectorAll('[data-svc]').length === 3 && d.querySelectorAll('[data-svc]'), 'three service cards');
  if (!d.getElementById('next').disabled) throw new Error('Continue should be disabled before choosing a service');
  cards[0].click();
  d.getElementById('next').click();

  const days = await wait(() => d.querySelectorAll('[data-d]').length > 5 && d.querySelectorAll('[data-d]'), 'day strip');
  if (days.length !== 31) throw new Error('expected 31 days (today + 30), got ' + days.length);
  const slot = await wait(() => d.querySelector('.slot:not([disabled])'), 'available times');
  if (!d.getElementById('next').disabled) throw new Error('Continue should be disabled before choosing a time');
  slot.click();
  const chosen = slot.textContent;
  d.getElementById('next').click();

  await wait(() => d.getElementById('nm'), 'details form');
  if (!main().includes(chosen)) throw new Error('summary should show the chosen time ' + chosen);
  d.getElementById('req').click();                        // empty form: must be rejected by validation
  if (!d.getElementById('err').textContent) throw new Error('empty form should show an error');
  type('nm', 'Priya Nair'); type('ph', '+91 98765 43210'); type('nt', 'Window seat please');
  const btn = d.getElementById('req'); btn.click(); btn.click(); btn.click();   // impatient triple click
  await wait(() => /SALON-\d+/.test(main()), 'confirmation');
  const m = main();
  if (!/not confirmed yet/i.test(m) || !/Pending/.test(m)) throw new Error('confirmation must say pending / not confirmed');
  if (/Booking confirmed/i.test(m)) throw new Error('must not claim the booking is confirmed');
  const link = d.getElementById('lnk').value;
  console.log(JSON.stringify({ ref: m.match(/SALON-\d+/)[0], link, time: chosen }));
  process.exit(0);
})().catch((e) => { console.error('UI TEST FAILED:', e.message); process.exit(1); });
