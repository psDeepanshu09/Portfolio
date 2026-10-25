"""End-to-end tests: real API code + real database logic (+ the real booking page via jsdom).

    pip install pytest "psycopg[binary]" pgserver
    pytest tests -v
The UI test needs jsdom (npm i jsdom, once, anywhere on NODE_PATH) and is skipped without it.
"""
import hashlib, json, os, pathlib, re, socket, subprocess, sys, time, urllib.error, urllib.request, uuid
import psycopg, pytest, pgserver

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import postgrest_shim as shim

TOKEN_SECRET = "unit-test-secret-0123456789abcdef"
SEEN = []      # every response body, to prove no secrets leak


@pytest.fixture(scope="session")
def env(tmp_path_factory):
    srv = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    uri = srv.get_uri()
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute("create role anon nologin; create role authenticated nologin")
        c.execute("create role service_role nologin bypassrls")           # what Supabase provides
        c.execute((ROOT / "supabase/migrations/0001_init.sql").read_text())
        c.execute((ROOT / "supabase/seed.sql").read_text())
        c.execute("grant usage on schema public to service_role; grant all on all tables in schema public to service_role")
    rest = shim.start(uri)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    e = dict(os.environ, PORT=str(port), SUPABASE_URL="http://127.0.0.1:%d" % rest.server_address[1],
             SUPABASE_SERVICE_ROLE_KEY=shim.KEY, TOKEN_SECRET=TOKEN_SECRET)
    p = subprocess.Popen(["node", str(ROOT / "dev/server.js")], env=e, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        try: urllib.request.urlopen(base + "/book.html"); break
        except Exception: time.sleep(0.1)
    yield {"base": base, "uri": uri, "env": e}
    p.terminate()


@pytest.fixture(autouse=True)
def _clean(env):
    with psycopg.connect(env["uri"], autocommit=True) as c:
        c.execute("truncate bookings; delete from days_off; update services set is_active = true")


@pytest.fixture
def db(env):
    c = psycopg.connect(env["uri"], autocommit=True)
    c.execute("truncate bookings; delete from days_off")
    yield c
    c.close()


def call(env, method, path, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(env["base"] + path, data=data, method=method,
                                 headers=dict({"Content-Type": "application/json"}, **(headers or {})))
    try:
        with urllib.request.urlopen(req) as r: code, text = r.status, r.read().decode()
    except urllib.error.HTTPError as e: code, text = e.code, e.read().decode()
    SEEN.append(text)
    return code, json.loads(text)


@pytest.fixture
def cfg(env):
    return call(env, "GET", "/api/config")[1]


def first_slots(env, cfg, svc_index=0, plus=3):
    svc = cfg["services"][svc_index]["id"]
    y, m, d = map(int, cfg["today"].split("-"))
    import datetime as dt
    day = (dt.date(y, m, d) + dt.timedelta(days=plus)).isoformat()
    return svc, call(env, "GET", "/api/slots?service=%s&date=%s" % (svc, day))[1]["slots"]


def payload(svc, start, **kw):
    return dict({"service_id": svc, "start": start, "name": "Rahul Sharma", "phone": "98765 43210",
                 "notes": "", "idempotency_key": str(uuid.uuid4()), "website": ""}, **kw)


# ── config and availability ──────────────────────────────────────────
def test_config_lists_services_and_schedule(env, cfg):
    assert [s["name"] for s in cfg["services"]] == ["Haircut and style", "Colour", "Hair spa"]
    assert set(cfg["services"][0]) == {"id", "name", "description", "price", "duration_minutes"}
    assert cfg["timezone"] == "Asia/Kolkata" and cfg["max_advance_days"] == 30
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", cfg["today"]) and cfg["closed_weekdays"] == []


def test_inactive_services_hidden_and_days_off_exposed(env, db):
    db.execute("update services set is_active=false where name='Hair spa'")
    day = db.execute("select (now() at time zone 'Asia/Kolkata')::date + 5").fetchone()[0].isoformat()
    db.execute("insert into days_off values (%s,'Holiday')", (day,))
    try:
        c = call(env, "GET", "/api/config")[1]
        assert len(c["services"]) == 2 and day in c["days_off"]
    finally:
        db.execute("update services set is_active=true")


def test_slots_endpoint(env, cfg):
    svc, slots = first_slots(env, cfg)
    assert len(slots) > 5 and all(s.endswith("+00:00") or "T" in s for s in slots)
    assert call(env, "GET", "/api/slots?service=nope&date=2026-10-30")[0] == 400
    assert call(env, "GET", "/api/slots?service=%s&date=2026-13-45" % svc)[0] == 400
    assert call(env, "GET", "/api/slots?service=%s&date=2020-01-01" % svc)[1]["slots"] == []     # past dates: nothing to book


# ── creating a booking request ───────────────────────────────────────
def test_book_creates_pending_request_and_token_works(env, cfg, db):
    svc, slots = first_slots(env, cfg)
    code, r = call(env, "POST", "/api/book", payload(svc, slots[3], notes="Window seat"))
    assert code == 200 and r["status"] == "pending" and r["reference"].startswith("SALON-")
    row = db.execute("select status, customer_phone, customer_notes, notification_status, access_token_hash, client_ip_hash from bookings").fetchone()
    assert row[:4] == ("pending", "9876543210", "Window seat", "pending")      # phone normalised, owner not yet notified
    assert row[4] == hashlib.sha256(r["token"].encode()).hexdigest() and r["token"] != row[4]   # only the hash is stored
    assert re.fullmatch(r"[0-9a-f]{64}", row[5])                                  # raw IP never stored
    code, s = call(env, "POST", "/api/status", {"ref": r["reference"], "t": r["token"]})
    assert code == 200 and s["status"] == "pending" and set(s) == {"booking_reference", "service_name", "price", "duration_minutes", "appointment_start", "appointment_end", "status"}
    assert call(env, "POST", "/api/status", {"ref": r["reference"], "t": "x" * 43})[0] == 404          # wrong token
    assert call(env, "POST", "/api/status", {"ref": "SALON-1", "t": r["token"]})[0] == 404             # wrong reference
    assert call(env, "POST", "/api/status", {"ref": "garbage", "t": "short"})[0] == 404


def test_duplicate_submission_returns_same_booking_and_token(env, cfg, db):
    svc, slots = first_slots(env, cfg)
    p = payload(svc, slots[2])
    a, b = call(env, "POST", "/api/book", p)[1], call(env, "POST", "/api/book", p)[1]
    assert a["reference"] == b["reference"] and a["token"] == b["token"]
    assert db.execute("select count(*) from bookings").fetchone()[0] == 1


def test_taken_slot_returns_409(env, cfg, db):
    svc, slots = first_slots(env, cfg)
    ref = call(env, "POST", "/api/book", payload(svc, slots[4]))[1]["reference"]
    bid = db.execute("select id from bookings where booking_reference=%s", (ref,)).fetchone()[0]
    assert db.execute("select result from confirm_booking(%s,1)", (bid,)).fetchone()[0] == "confirmed"
    code, r = call(env, "POST", "/api/book", payload(svc, slots[4], phone="9123456789"))
    assert (code, r["error"]) == (409, "slot_unavailable")
    assert slots[4] not in first_slots(env, cfg)[1]                              # and it vanished from the website


def test_forged_times_rejected(env, cfg, db):
    import datetime as dt
    svc, slots = first_slots(env, cfg)
    s0 = dt.datetime.fromisoformat(slots[0])
    for bad in ("2020-01-01T10:00:00Z", "not a date",
                (s0 - dt.timedelta(hours=5)).isoformat(),            # before opening
                (s0 + dt.timedelta(minutes=10)).isoformat()):        # not on the 30-minute grid
        code, r = call(env, "POST", "/api/book", payload(svc, bad))
        assert code in (400, 409), (bad, code)
    assert db.execute("select count(*) from bookings").fetchone()[0] == 0


@pytest.mark.parametrize("patch,err", [({"phone": "12345"}, "invalid_phone"), ({"name": "x"}, "invalid_name"),
                                        ({"notes": "n" * 501}, "invalid_notes")])
def test_validation_errors(env, cfg, patch, err):
    svc, slots = first_slots(env, cfg)
    assert call(env, "POST", "/api/book", payload(svc, slots[0], **patch)) == (400, {"error": err})


def test_malformed_requests(env, cfg):
    svc, slots = first_slots(env, cfg)
    assert call(env, "POST", "/api/book", payload(svc, slots[0], website="http://spam"))[0] == 400    # honeypot
    assert call(env, "POST", "/api/book", payload(svc, slots[0], idempotency_key="short"))[0] == 400
    assert call(env, "POST", "/api/book", payload("not-a-uuid", slots[0]))[0] == 400
    assert call(env, "POST", "/api/book", raw=b"{broken json")[0] == 400
    assert call(env, "POST", "/api/book", raw=b"[]")[0] == 400
    assert call(env, "POST", "/api/book", payload(str(uuid.uuid4()), slots[0]))[1]["error"] == "service_not_found"
    assert call(env, "GET", "/api/book")[0] == 405 and call(env, "POST", "/api/config", {})[0] == 405


def test_rate_limit_per_phone(env, cfg):
    svc, slots = first_slots(env, cfg)
    for i in range(3): assert call(env, "POST", "/api/book", payload(svc, slots[i]))[0] == 200
    assert call(env, "POST", "/api/book", payload(svc, slots[5])) == (429, {"error": "rate_limited"})


def test_status_follows_owner_decisions(env, cfg, db):
    svc, slots = first_slots(env, cfg)
    r = call(env, "POST", "/api/book", payload(svc, slots[6]))[1]
    bid = db.execute("select id from bookings").fetchone()[0]
    st = lambda: call(env, "POST", "/api/status", {"ref": r["reference"], "t": r["token"]})[1]["status"]
    db.execute("select * from confirm_booking(%s,1)", (bid,)); assert st() == "confirmed"
    db.execute("select * from cancel_booking(%s,1)", (bid,)); assert st() == "cancelled"
    r2 = call(env, "POST", "/api/book", payload(svc, slots[8], phone="9123456789"))[1]
    db.execute("update bookings set expires_at = now() - interval '1 second' where booking_reference=%s", (r2["reference"],))
    assert call(env, "POST", "/api/status", {"ref": r2["reference"], "t": r2["token"]})[1]["status"] == "expired"


def test_server_errors_do_not_leak_details(env, cfg):
    svc, slots = first_slots(env, cfg)
    bad = dict(env["env"], SUPABASE_SERVICE_ROLE_KEY="wrong-key", PORT="0")
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    bad["PORT"] = str(port)
    p = subprocess.Popen(["node", str(ROOT / "dev/server.js")], env=bad, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try: urllib.request.urlopen("http://127.0.0.1:%d/book.html" % port); break
            except Exception: time.sleep(0.1)
        code, r = call({"base": "http://127.0.0.1:%d" % port}, "GET", "/api/config")
        assert (code, r) == (500, {"error": "server_error"})
    finally:
        p.terminate()


# ── the real booking page ────────────────────────────────────────────
def test_booking_page_flow_in_jsdom(env, db):
    node_env = dict(env["env"], BASE=env["base"], NODE_PATH=os.environ.get("NODE_PATH", "") + os.pathsep + "/home/claude/node_modules")
    if subprocess.run(["node", "-e", "require('jsdom')"], env=node_env, capture_output=True).returncode:
        pytest.skip("jsdom not installed")
    out = subprocess.run(["node", str(ROOT / "tests/ui_smoke.js")], env=node_env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr + out.stdout
    info = json.loads(out.stdout.strip().splitlines()[-1])
    rows = db.execute("select booking_reference, customer_name, customer_phone, customer_notes, status from bookings").fetchall()
    assert rows == [(info["ref"], "Priya Nair", "9876543210", "Window seat please", "pending")]      # triple-click made ONE booking
    frag = dict(x.split("=") for x in info["link"].split("#")[1].split("&"))
    assert call(env, "POST", "/api/status", {"ref": frag["ref"], "t": frag["t"]})[1]["status"] == "pending"   # saved link works


def test_zz_no_secrets_in_any_response(env):
    blob = "\n".join(SEEN)
    for secret in (shim.KEY, TOKEN_SECRET, "access_token_hash", "service_role", "client_ip_hash", "127.0.0.1", "SUPABASE"):
        assert secret not in blob, secret
