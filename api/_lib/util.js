const crypto = require('crypto');

function send(res, code, obj, headers) {
  res.statusCode = code;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  if (headers) for (const k of Object.keys(headers)) res.setHeader(k, headers[k]);
  res.end(JSON.stringify(obj));
}

// Works with Vercel (body already parsed) and with a plain Node server (read the stream).
async function readBody(req) {
  if (req.body !== undefined && req.body !== null) {
    if (typeof req.body === 'string') { try { return JSON.parse(req.body); } catch (_) { return null; } }
    return req.body;
  }
  const chunks = []; let size = 0;
  for await (const c of req) { size += c.length; if (size > 20000) return null; chunks.push(c); }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8') || 'null'); } catch (_) { return null; }
}

function secret() {
  const s = process.env.TOKEN_SECRET;
  if (!s || s.length < 16) throw new Error('TOKEN_SECRET is missing or too short');
  return s;
}
const hmac = (label, value, enc) => crypto.createHmac('sha256', secret()).update(label + ':' + value).digest(enc);

// The customer's secret status token is derived from their form's idempotency key, so a retried
// submission gets the same token back. Only the SHA-256 of it is stored in the database.
const tokenFor = (key) => hmac('token', key, 'base64url');
const sha256 = (t) => crypto.createHash('sha256').update(t).digest('hex');

function ipHash(req) {
  const fwd = String(req.headers['x-forwarded-for'] || '').split(',')[0];
  const ip = String(req.headers['x-real-ip'] || fwd || (req.socket && req.socket.remoteAddress) || '').trim();
  return hmac('ip', ip, 'hex');   // never store the raw IP
}

function fail(res, e) {
  console.error('api_error:', e && e.message);      // details stay in the server log
  send(res, 500, { error: 'server_error' });         // the browser never sees them
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DATE = /^\d{4}-\d{2}-\d{2}$/;
const validDate = (s) => {
  if (!DATE.test(s)) return false;
  const d = new Date(s + 'T00:00:00Z');
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s;   // rejects 2026-13-45, 2026-02-31
};

module.exports = { send, readBody, tokenFor, sha256, ipHash, fail, UUID, validDate };
