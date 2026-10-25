// Talks to Supabase's REST API with the SERVICE-ROLE key. Server-side only: never import this in browser code.
function env(name) {
  const v = process.env[name];
  if (!v) throw new Error('Missing environment variable ' + name);
  return v;
}

async function pg(path, { method = 'GET', body } = {}) {
  const base = env('SUPABASE_URL').replace(/\/$/, '');
  const key = env('SUPABASE_SERVICE_ROLE_KEY');
  const r = await fetch(base + '/rest/v1/' + path, {
    method,
    headers: { apikey: key, Authorization: 'Bearer ' + key, 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch (_) { /* not JSON */ }
  if (!r.ok) {
    const e = new Error((data && data.message) || 'db_error_' + r.status);
    e.status = r.status;
    throw e;
  }
  return data;
}

const rpc = (fn, args) => pg('rpc/' + fn, { method: 'POST', body: args || {} });

module.exports = { pg, rpc };
