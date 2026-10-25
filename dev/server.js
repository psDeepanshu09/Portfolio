// Local development server: serves /public and runs the /api functions. Usage: node dev/server.js
const http = require('http'), fs = require('fs'), path = require('path');
const root = path.join(__dirname, '..');
try {   // tiny .env loader (no dependencies)
  for (const line of fs.readFileSync(path.join(root, '.env'), 'utf8').split('\n')) {
    const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (m && !line.trim().startsWith('#') && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
} catch (_) { /* no .env file */ }
const MIME = { '.html': 'text/html', '.png': 'image/png', '.jpg': 'image/jpeg', '.mp4': 'video/mp4', '.css': 'text/css', '.js': 'text/javascript' };
const API = ['config', 'slots', 'book', 'status'];
http.createServer((req, res) => {
  const u = new URL(req.url, 'http://localhost');
  const m = u.pathname.match(/^\/api\/([a-z]+)$/);
  if (m && API.includes(m[1])) {
    return Promise.resolve(require('../api/' + m[1])(req, res)).catch((e) => {   // like Vercel: an error becomes a 500, not a crash
      console.error('unhandled:', e && e.message);
      if (!res.headersSent) { res.statusCode = 500; res.end('{"error":"server_error"}'); }
    });
  }
  let p = path.normalize(decodeURIComponent(u.pathname)).replace(/^(\.\.[/\\])+/, '');
  if (p === '/' || p === path.sep) p = '/index.html';
  const file = path.join(root, 'public', p);
  if (!file.startsWith(path.join(root, 'public')) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.statusCode = 404; return res.end('Not found'); }
  res.setHeader('Content-Type', MIME[path.extname(file)] || 'application/octet-stream');
  fs.createReadStream(file).pipe(res);
}).listen(process.env.PORT || 3000, () => console.log('http://localhost:' + (process.env.PORT || 3000)));
