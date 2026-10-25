const { rpc } = require('./_lib/db');
const { send, readBody, tokenFor, sha256, ipHash, fail, UUID } = require('./_lib/util');

const KEY = /^[A-Za-z0-9_-]{16,64}$/;
const KNOWN = { slot_unavailable: 409, rate_limited: 429, invalid_name: 400, invalid_phone: 400, invalid_notes: 400, service_not_found: 404 };

function cleanPhone(p) {
  let d = String(p || '').replace(/[\s\-().]/g, '');
  if (d.startsWith('+91')) d = d.slice(3);
  else if (/^91\d{10}$/.test(d)) d = d.slice(2);
  else if (/^0\d{10}$/.test(d)) d = d.slice(1);
  return /^[6-9]\d{9}$/.test(d) ? d : null;
}

// POST /api/book  -> saves a PENDING request. The database rechecks the slot atomically.
module.exports = async (req, res) => {
  if (req.method !== 'POST') return send(res, 405, { error: 'method_not_allowed' }, { Allow: 'POST' });
  const b = await readBody(req);
  if (!b || typeof b !== 'object' || Array.isArray(b)) return send(res, 400, { error: 'invalid_request' });
  if (b.website) return send(res, 400, { error: 'invalid_request' });              // hidden spam-trap field

  const name = typeof b.name === 'string' ? b.name.trim() : '';
  const notes = typeof b.notes === 'string' ? b.notes.trim() : '';
  const phone = cleanPhone(b.phone);
  const start = typeof b.start === 'string' ? Date.parse(b.start) : NaN;
  if (typeof b.service_id !== 'string' || !UUID.test(b.service_id) || Number.isNaN(start)
      || typeof b.idempotency_key !== 'string' || !KEY.test(b.idempotency_key)) return send(res, 400, { error: 'invalid_request' });
  if (name.length < 2 || name.length > 80) return send(res, 400, { error: 'invalid_name' });
  if (!phone) return send(res, 400, { error: 'invalid_phone' });
  if (notes.length > 500) return send(res, 400, { error: 'invalid_notes' });

  try {
    const token = tokenFor(b.idempotency_key);
    const row = await rpc('create_booking_request', {
      p_service_id: b.service_id,
      p_start: new Date(start).toISOString(),
      p_name: name, p_phone: phone, p_notes: notes,
      p_token_hash: sha256(token),
      p_idempotency_key: b.idempotency_key,
      p_ip_hash: ipHash(req),
    });
    // Stage 4 sends the Telegram message here. Until then the booking stays notification_status='pending'
    // and the retry job picks it up, so nothing is lost.
    send(res, 200, {
      reference: row.booking_reference, status: row.status, token,
      service_name: row.service_name, price: row.price, duration_minutes: row.duration_minutes,
      start: row.appointment_start, end: row.appointment_end,
    });
  } catch (e) {
    if (KNOWN[e.message]) return send(res, KNOWN[e.message], { error: e.message });
    fail(res, e);
  }
};
