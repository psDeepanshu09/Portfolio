"""Database tests for the salon booking logic. Run against a throwaway Postgres:

    pip install pytest "psycopg[binary]" pgserver
    pytest supabase/tests -v
"""
import datetime as dt, threading, time, uuid
import pathlib, pytest, psycopg, pgserver

ROOT = pathlib.Path(__file__).resolve().parents[1]
TZ = "Asia/Kolkata"


@pytest.fixture(scope="session")
def uri(tmp_path_factory):
    srv = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    with psycopg.connect(srv.get_uri(), autocommit=True) as c:
        for role in ("anon", "authenticated", "service_role"):   # Supabase creates these for you
            c.execute(f"create role {role} nologin")
        c.execute((ROOT / "migrations/0001_init.sql").read_text())
        c.execute((ROOT / "seed.sql").read_text())
    return srv.get_uri()


@pytest.fixture
def db(uri):
    c = psycopg.connect(uri, autocommit=True)
    c.execute("truncate bookings, telegram_updates; delete from days_off; update services set is_active = true")
    yield c
    c.close()


def conn(uri):
    return psycopg.connect(uri, autocommit=False)


def svc(db, name):
    return db.execute("select id from services where name ilike %s", (name + "%",)).fetchone()[0]


def local_day(db, plus):
    return db.execute("select (now() at time zone %s)::date + %s", (TZ, plus)).fetchone()[0]


def slots(db, service, day):
    return [r[0] for r in db.execute("select slot_start from get_available_slots(%s,%s)", (service, day))]


def hhmm(ts):
    return ts.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30))).strftime("%H:%M")


def book(db, service, start, phone="9876543210", key=None, name="Rahul Sharma", ip=None):
    key = key or str(uuid.uuid4())
    return db.execute(
        "select id, booking_reference, status from create_booking_request(%s,%s,%s,%s,%s,%s,%s,%s)",
        (service, start, name, phone, "", uuid.uuid4().hex, key, ip)).fetchone()


def err(db, fn):
    with pytest.raises(psycopg.errors.Error) as e:
        fn()
    return str(e.value)


@pytest.fixture
def day(db):
    return local_day(db, 3)


# 1 ── availability rules ────────────────────────────────────────────────
def test_slots_follow_hours_breaks_and_interval(db, day):
    cut = svc(db, "Haircut")
    s = [hhmm(x) for x in slots(db, cut, day)]
    open_t, close_t = db.execute(
        "select to_char(open_time,'HH24:MI'), to_char(close_time,'HH24:MI') from opening_hours where weekday=extract(dow from %s::date)", (day,)).fetchone()
    assert s[0] == open_t
    assert all(x[-2:] in ("00", "30") for x in s)                # 30-minute grid
    assert "13:30" not in s and "14:00" not in s                  # would touch the 14:00-14:30 break
    assert "14:30" in s and "13:00" in s
    assert max(s) <= close_t                                      # nothing starts too late
    last = dt.datetime.strptime(s[-1], "%H:%M") + dt.timedelta(minutes=45)
    assert last.strftime("%H:%M") <= close_t                      # last appointment ends by closing time


def test_longer_service_has_fewer_slots(db, day):
    assert len(slots(db, svc(db, "Colour"), day)) < len(slots(db, svc(db, "Haircut"), day))


def test_day_off_has_no_slots(db, day):
    db.execute("insert into days_off values (%s,'Holiday')", (day,))
    assert slots(db, svc(db, "Haircut"), day) == []


def test_hours_cannot_cross_midnight(db):
    # closing before opening (which is what crossing midnight looks like) is rejected
    assert "violates check constraint" in err(db, lambda: db.execute("update opening_hours set close_time='02:00' where weekday=1"))
    # and so is closing after 23:59
    assert "violates check constraint" in err(db, lambda: db.execute("update opening_hours set close_time='23:59:30' where weekday=1"))


# 2 ── booking requests ──────────────────────────────────────────────────
def test_valid_request_is_pending_and_does_not_block(db, day):
    cut = svc(db, "Haircut"); t = slots(db, cut, day)[4]
    _, ref, status = book(db, cut, t)
    assert status == "pending" and ref.startswith("SALON-")
    assert t in slots(db, cut, day)                               # pending requests never block a slot
    assert book(db, cut, t, phone="9123456789")[2] == "pending"   # a second customer can request it too


@pytest.mark.parametrize("what", ["off_grid", "before_open", "during_break", "past", "too_far", "inactive"])
def test_invalid_slots_rejected(db, day, what):
    cut = svc(db, "Haircut")
    base = slots(db, cut, day)[0]
    if what == "off_grid":      t = base + dt.timedelta(minutes=10)
    elif what == "before_open": t = base - dt.timedelta(hours=2)
    elif what == "during_break":
        t = next(x for x in slots(db, cut, day) if hhmm(x) == "13:00") + dt.timedelta(minutes=60)  # 14:00
    elif what == "past":        t = base - dt.timedelta(days=5)
    elif what == "too_far":     t = base + dt.timedelta(days=60)
    else:
        db.execute("update services set is_active=false where id=%s", (cut,)); t = base
    expected = "service_not_found" if what == "inactive" else "slot_unavailable"
    assert expected in err(db, lambda: book(db, cut, t))


def test_bad_customer_input_rejected(db, day):
    cut = svc(db, "Haircut"); t = slots(db, cut, day)[0]
    assert "invalid_phone" in err(db, lambda: book(db, cut, t, phone="12345"))
    assert "invalid_name" in err(db, lambda: book(db, cut, t, name="x"))


def test_idempotent_submission(db, day, uri):
    cut = svc(db, "Haircut"); t = slots(db, cut, day)[2]
    a = book(db, cut, t, key="same-key"); b = book(db, cut, t, key="same-key")
    assert a[0] == b[0]
    # two identical submissions racing each other still make one row
    out = []
    def go():
        with conn(uri) as c:
            out.append(book(c, cut, slots(c, cut, day)[3], phone="9000000001", key="race-key")[0]); c.commit()
    ts = [threading.Thread(target=go) for _ in range(2)]
    [x.start() for x in ts]; [x.join() for x in ts]
    assert len(set(out)) == 1


def test_rate_limit_per_phone(db, day):
    cut = svc(db, "Haircut"); sl = slots(db, cut, day)
    for i in range(3): book(db, cut, sl[i])
    assert "rate_limited" in err(db, lambda: book(db, cut, sl[3]))


def test_rate_limit_per_ip(db, day):
    cut = svc(db, "Haircut"); sl = slots(db, cut, day)
    for i in range(10): book(db, cut, sl[i], phone=f"90000000{i:02d}", ip="iphash")
    assert "rate_limited" in err(db, lambda: book(db, cut, sl[11], phone="9111111111", ip="iphash"))


# 3 ── owner actions ─────────────────────────────────────────────────────
def confirm(db, bid, who=111): return db.execute("select * from confirm_booking(%s,%s)", (bid, who)).fetchone()
def decline(db, bid, who=111): return db.execute("select * from decline_booking(%s,%s)", (bid, who)).fetchone()


def test_confirm_blocks_overlapping_slots_for_any_service(db, day):
    cut, col = svc(db, "Haircut"), svc(db, "Colour")
    t = slots(db, cut, day)[6]
    bid = book(db, cut, t)[0]
    before = set(slots(db, col, day))
    assert confirm(db, bid) == ("confirmed", "confirmed")
    after = set(slots(db, col, day))
    end = t + dt.timedelta(minutes=45)
    assert before - after                                          # some slots disappeared
    for s in after:                                                # none overlaps [t, t+45min)
        assert not (s < end and s + dt.timedelta(minutes=120) > t)
    assert t not in slots(db, cut, day)


def test_decline_keeps_slot_open_and_other_can_still_be_confirmed(db, day):
    cut = svc(db, "Haircut"); t = slots(db, cut, day)[5]
    a = book(db, cut, t)[0]; b = book(db, cut, t, phone="9123456789")[0]
    assert decline(db, a) == ("declined", "declined")
    assert t in slots(db, cut, day)
    assert confirm(db, b)[0] == "confirmed"
    assert t not in slots(db, cut, day)


def test_stale_and_duplicate_callbacks_do_nothing(db, day):
    cut = svc(db, "Haircut"); sl = slots(db, cut, day)
    a = book(db, cut, sl[1])[0]
    assert confirm(db, a)[0] == "confirmed"
    assert confirm(db, a) == ("not_pending", "confirmed")         # double tap
    assert decline(db, a) == ("not_pending", "confirmed")         # stale Decline button
    b = book(db, cut, sl[8], phone="9123456789")[0]
    decline(db, b)
    assert confirm(db, b) == ("not_pending", "declined")
    assert confirm(db, str(uuid.uuid4()))[0] == "not_found"


def test_confirm_conflict_when_overlapping_confirmed_exists(db, day):
    cut = svc(db, "Haircut"); sl = slots(db, cut, day)
    a = book(db, cut, sl[2])[0]
    b = book(db, cut, sl[3], phone="9123456789")[0]               # starts 30 min later: overlaps a 45-min cut
    assert confirm(db, a)[0] == "confirmed"
    assert confirm(db, b) == ("conflict", "pending")
    assert db.execute("select status from bookings where id=%s", (b,)).fetchone()[0] == "pending"


def test_two_owner_confirmations_at_the_same_instant(db, day, uri):
    cut = svc(db, "Haircut"); sl = slots(db, cut, day)
    a = book(db, cut, sl[2])[0]
    b = book(db, cut, sl[3], phone="9123456789")[0]
    ca, cb = conn(uri), conn(uri)
    try:
        ra = ca.execute("select * from confirm_booking(%s,1)", (a,)).fetchone()   # txn A open, not yet committed
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("b", cb.execute("select * from confirm_booking(%s,1)", (b,)).fetchone()))
        th.start(); time.sleep(0.8)
        assert th.is_alive()                                       # B is waiting on the database constraint
        ca.commit(); th.join(5); cb.commit()
        assert ra == ("confirmed", "confirmed") and out["b"] == ("conflict", "pending")
        n = db.execute("select count(*) from bookings where status='confirmed'").fetchone()[0]
        assert n == 1
    finally:                                                       # never leave locks behind
        ca.rollback(); cb.rollback(); ca.close(); cb.close()


def test_confirm_rejects_expired_and_changed_rules(db, day):
    cut = svc(db, "Haircut"); sl = slots(db, cut, day)
    a = book(db, cut, sl[1])[0]
    db.execute("update bookings set expires_at = now() - interval '1 minute' where id=%s", (a,))
    assert confirm(db, a) == ("expired", "expired")
    b = book(db, cut, sl[4], phone="9123456789")[0]
    db.execute("insert into days_off values (%s,'Owner added holiday')", (day,))
    assert confirm(db, b) == ("invalid_slot", "pending")


def test_cancel_confirmed_frees_slot(db, day):
    cut = svc(db, "Haircut"); t = slots(db, cut, day)[3]
    a = book(db, cut, t)[0]; confirm(db, a)
    assert db.execute("select * from cancel_booking(%s,1)", (a,)).fetchone() == ("cancelled", "cancelled")
    assert t in slots(db, cut, day)


# 4 ── database-level safety nets ────────────────────────────────────────
def test_constraint_blocks_overlapping_confirmed_even_with_raw_sql(db, day):
    cut = svc(db, "Haircut"); sl = slots(db, cut, day)
    a = book(db, cut, sl[2])[0]; b = book(db, cut, sl[3], phone="9123456789")[0]
    confirm(db, a)
    e = err(db, lambda: db.execute("update bookings set status='confirmed' where id=%s", (b,)))
    assert "bookings_no_overlap_when_confirmed" in e


def test_illegal_status_changes_blocked(db, day):
    cut = svc(db, "Haircut"); a = book(db, cut, slots(db, cut, day)[2])[0]
    decline(db, a)
    assert "invalid_status_transition" in err(db, lambda: db.execute("update bookings set status='confirmed' where id=%s", (a,)))
    assert "immutable_field" in err(db, lambda: db.execute("update bookings set booking_reference='SALON-1' where id=%s", (a,)))


# 5 ── customer status page ──────────────────────────────────────────────
def test_status_needs_matching_token(db, day):
    cut = svc(db, "Haircut"); t = slots(db, cut, day)[2]
    ref = db.execute("select * from create_booking_request(%s,%s,'Asha Verma','9876500000','', 'goodhash','k1',null)", (cut, t)).fetchone()[1]
    ok = db.execute("select * from get_booking_status(%s,'goodhash')", (ref,)).fetchall()
    assert len(ok) == 1 and ok[0][-1] == "pending"
    assert db.execute("select * from get_booking_status(%s,'badhash')", (ref,)).fetchall() == []
    assert db.execute("select * from get_booking_status('SALON-1','goodhash')").fetchall() == []
    assert len(ok[0]) == 7                                         # no phone, name or notes in the response


def test_expiry(db, day):
    cut = svc(db, "Haircut"); t = slots(db, cut, day)[2]
    ref = db.execute("select * from create_booking_request(%s,%s,'Asha Verma','9876500000','', 'h2','k2',null)", (cut, t)).fetchone()[1]
    db.execute("update bookings set expires_at = now() - interval '1 second'")
    assert db.execute("select status from get_booking_status(%s,'h2')", (ref,)).fetchone()[0] == "expired"  # shown as expired immediately
    assert db.execute("select expire_pending_bookings()").fetchone()[0] == 1
    assert db.execute("select status from bookings").fetchone()[0] == "expired"
    assert t in slots(db, cut, day)


# 6 ── Telegram support ──────────────────────────────────────────────────
def test_replayed_telegram_update_ignored(db):
    assert db.execute("select record_telegram_update(5001)").fetchone()[0] is True
    assert db.execute("select record_telegram_update(5001)").fetchone()[0] is False


def test_failed_notification_is_retried(db, day):
    cut = svc(db, "Haircut"); a = book(db, cut, slots(db, cut, day)[2])[0]
    db.execute("select mark_notification_result(%s,false,null,'telegram down')", (a,))
    assert db.execute("select count(*) from claim_unsent_notifications(10)").fetchone()[0] == 0   # too soon
    db.execute("update bookings set updated_at = now() - interval '5 minutes'")
    assert db.execute("select count(*) from claim_unsent_notifications(10)").fetchone()[0] == 1
    db.execute("select mark_notification_result(%s,true,777,null)", (a,))
    assert db.execute("select notification_status, telegram_message_id from bookings").fetchone() == ("sent", 777)
    db.execute("update bookings set updated_at = now() - interval '5 minutes'")
    assert db.execute("select count(*) from claim_unsent_notifications(10)").fetchone()[0] == 0


# 7 ── access control (Row Level Security) ───────────────────────────────
def test_browser_roles_cannot_touch_bookings_or_functions(db, day):
    cut = svc(db, "Haircut"); book(db, cut, slots(db, cut, day)[2])
    for role in ("anon", "authenticated"):
        db.execute(f"set role {role}")
        try:
            assert len(db.execute("select * from services").fetchall()) == 3           # public menu is readable
            for sql in ("select * from bookings", "select * from salon_settings",
                        "insert into bookings (customer_name) values ('x')",
                        "select * from confirm_booking(gen_random_uuid(),1)",
                        "select * from create_booking_request(gen_random_uuid(),now(),'a','b','c','d','e','f')"):
                assert "permission denied" in err(db, lambda: db.execute(sql))
        finally:
            db.execute("reset role")
    db.execute("update services set is_active=false where name='Colour'")
    db.execute("set role anon")
    assert len(db.execute("select * from services").fetchall()) == 2                   # inactive hidden by RLS
    db.execute("reset role")
    db.execute("set role service_role")
    assert db.execute("select count(*) from get_available_slots(%s,%s)", (cut, day)).fetchone()[0] > 0
    db.execute("reset role")
