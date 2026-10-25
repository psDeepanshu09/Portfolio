"""A tiny stand-in for Supabase's REST layer (PostgREST), for tests only.
It maps /rest/v1/rpc/<fn> and simple table GETs onto a local Postgres and runs every
request as the `service_role` database role, so the real grants and RLS are exercised."""
import datetime, decimal, json, re, threading, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import psycopg

TABLES = {"services", "salon_settings", "opening_hours", "days_off"}
IDENT = re.compile(r"^[a-z_]+$")
KEY = "test-service-key-123"


def enc(o):
    if isinstance(o, (datetime.datetime, datetime.date, datetime.time)):
        return o.isoformat()
    if isinstance(o, (decimal.Decimal, uuid.UUID)):
        return str(o)
    raise TypeError(type(o))


def make_handler(pg_uri):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send(self, code, obj):
            body = json.dumps(obj, default=enc).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            return self.headers.get("apikey") == KEY and self.headers.get("Authorization") == "Bearer " + KEY

        def _run(self, fn):
            if not self._authed():
                return self._send(401, {"message": "Invalid API key"})
            try:
                with psycopg.connect(pg_uri, autocommit=False) as c:
                    c.execute("set local role service_role")
                    code, out = fn(c)
                    c.commit()
                self._send(code, out)
            except psycopg.errors.InsufficientPrivilege as e:
                self._send(403, {"code": e.sqlstate, "message": e.diag.message_primary})
            except psycopg.errors.Error as e:
                self._send(400, {"code": e.sqlstate, "message": e.diag.message_primary})

        def do_POST(self):
            m = re.match(r"^/rest/v1/rpc/([a-z_]+)$", urlparse(self.path).path)
            if not m: return self._send(404, {"message": "not found"})
            args = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
            fn = m.group(1)

            def go(c):
                meta = c.execute("select p.proretset, t.typtype from pg_proc p join pg_type t on t.oid=p.prorettype "
                                 "where p.pronamespace='public'::regnamespace and p.proname=%s", (fn,)).fetchone()
                if not meta: return 404, {"message": "no such function"}
                names = list(args)
                assert all(IDENT.match(n) for n in names)
                sql = "select * from public.%s(%s)" % (fn, ", ".join("%s := %%s" % n for n in names))
                cur = c.execute(sql, [args[n] for n in names])
                cols = [d.name for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                if meta[0]: return 200, rows
                if meta[1] == "c": return 200, rows[0] if rows else None
                return 200, rows[0][cols[0]] if rows else None
            self._run(go)

        def do_GET(self):
            u = urlparse(self.path)
            m = re.match(r"^/rest/v1/([a-z_]+)$", u.path)
            if not m or m.group(1) not in TABLES: return self._send(404, {"message": "not found"})
            q = {k: v[0] for k, v in parse_qs(u.query).items()}

            def go(c):
                cols = [x for x in q.pop("select", "*").split(",")]
                order = q.pop("order", None)
                assert all(IDENT.match(x) or x == "*" for x in cols)
                where, params = [], []
                for k, v in q.items():
                    op, _, val = v.partition(".")
                    assert IDENT.match(k) and op in ("eq", "gte")
                    where.append("%s %s %%s" % (k, {"eq": "=", "gte": ">="}[op])); params.append(val)
                sql = "select %s from public.%s" % (", ".join(cols), m.group(1))
                if where: sql += " where " + " and ".join(where)
                if order: sql += " order by " + order.split(".")[0]
                cur = c.execute(sql, params)
                names = [d.name for d in cur.description]
                return 200, [dict(zip(names, r)) for r in cur.fetchall()]
            self._run(go)
    return H


def start(pg_uri):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(pg_uri))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
