const { rpc } = require('./_lib/db');
const { send, fail, UUID, validDate } = require('./_lib/util');

// GET /api/slots?service=<uuid>&date=YYYY-MM-DD  ->  { slots: [ISO start times] }
// The database applies hours, breaks, days off, durations and confirmed bookings.
module.exports = async (req, res) => {
  if (req.method !== 'GET') return send(res, 405, { error: 'method_not_allowed' }, { Allow: 'GET' });
  const q = new URL(req.url, 'http://localhost').searchParams;
  const service = q.get('service') || '', date = q.get('date') || '';
  if (!UUID.test(service) || !validDate(date)) return send(res, 400, { error: 'invalid_request' });
  try {
    const rows = await rpc('get_available_slots', { p_service_id: service, p_date: date });
    send(res, 200, { slots: rows.map((r) => r.slot_start) });
  } catch (e) { fail(res, e); }
};
