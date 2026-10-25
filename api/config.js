const { pg } = require('./_lib/db');
const { send, fail } = require('./_lib/util');

// Everything the booking page needs to start: services, booking window, closed days.
module.exports = async (req, res) => {
  if (req.method !== 'GET') return send(res, 405, { error: 'method_not_allowed' }, { Allow: 'GET' });
  try {
    const [services, settings, hours, off] = await Promise.all([
      pg('services?select=id,name,description,price,duration_minutes&is_active=eq.true&order=created_at.asc'),
      pg('salon_settings?select=timezone,max_advance_days&id=eq.1'),
      pg('opening_hours?select=weekday,is_open'),
      pg('days_off?select=day'),
    ]);
    if (!settings || !settings[0]) throw new Error('salon_settings row missing');
    const s = settings[0];
    const today = new Intl.DateTimeFormat('en-CA', { timeZone: s.timezone }).format(new Date());   // YYYY-MM-DD in salon time
    const open = hours.filter((h) => h.is_open).map((h) => h.weekday);
    send(res, 200, {
      timezone: s.timezone,
      today,
      max_advance_days: s.max_advance_days,
      closed_weekdays: [0, 1, 2, 3, 4, 5, 6].filter((d) => !open.includes(d)),
      days_off: off.map((d) => d.day).filter((d) => d >= today),
      services,
    });
  } catch (e) { fail(res, e); }
};
