const { rpc } = require('./_lib/db');
const { send, readBody, sha256, fail } = require('./_lib/util');

// POST /api/status {ref, t}  ->  the booking's public details, only if the secret token matches.
module.exports = async (req, res) => {
  if (req.method !== 'POST') return send(res, 405, { error: 'method_not_allowed' }, { Allow: 'POST' });
  const b = await readBody(req);
  if (!b || typeof b.ref !== 'string' || typeof b.t !== 'string'
      || !/^SALON-\d{1,10}$/.test(b.ref) || !/^[A-Za-z0-9_-]{20,100}$/.test(b.t)) return send(res, 404, { error: 'not_found' });
  try {
    const rows = await rpc('get_booking_status', { p_reference: b.ref, p_token_hash: sha256(b.t) });
    if (!rows.length) return send(res, 404, { error: 'not_found' });     // same answer for wrong ref and wrong token
    send(res, 200, rows[0]);
  } catch (e) { fail(res, e); }
};
