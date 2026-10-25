-- Salon booking system: schema, concurrency-safe booking logic, and security.
-- Assumes one booking at a time (one stylist / one chair).
-- Run in the Supabase SQL editor, or with `supabase db push`.

-- ───────────────────────── Tables ─────────────────────────

create sequence if not exists booking_ref_seq start 1001;

create table public.services (
  id               uuid primary key default gen_random_uuid(),
  name             text not null check (char_length(name) between 1 and 80),
  description      text not null default '' check (char_length(description) <= 500),
  price            integer not null check (price >= 0),            -- INR, whole rupees
  duration_minutes integer not null check (duration_minutes between 5 and 600),
  is_active        boolean not null default true,
  created_at       timestamptz not null default now()
);

-- Exactly one row (id = 1).
create table public.salon_settings (
  id                        smallint primary key default 1 check (id = 1),
  timezone                  text    not null default 'Asia/Kolkata',
  slot_interval_minutes     integer not null default 30  check (slot_interval_minutes between 5 and 120),
  min_advance_minutes       integer not null default 60  check (min_advance_minutes >= 0),
  max_advance_days          integer not null default 30  check (max_advance_days between 1 and 365),
  pending_expiry_hours      integer not null default 24  check (pending_expiry_hours between 1 and 720),
  max_pending_per_phone     integer not null default 3   check (max_pending_per_phone >= 1),
  max_requests_per_ip_hour  integer not null default 10  check (max_requests_per_ip_hour >= 1)
);
insert into public.salon_settings default values on conflict do nothing;

-- weekday: 0 = Sunday ... 6 = Saturday. Appointments never cross midnight.
create table public.opening_hours (
  weekday    smallint primary key check (weekday between 0 and 6),
  is_open    boolean not null default true,
  open_time  time not null,
  close_time time not null,
  check (close_time > open_time),
  check (close_time <= time '23:59')
);

-- weekday NULL = every day.
create table public.breaks (
  id         uuid primary key default gen_random_uuid(),
  weekday    smallint check (weekday between 0 and 6),
  start_time time not null,
  end_time   time not null,
  label      text,
  check (end_time > start_time)
);

create table public.days_off (
  day    date primary key,
  reason text
);

create table public.bookings (
  id                     uuid primary key default gen_random_uuid(),
  booking_reference      text not null unique default ('SALON-' || nextval('booking_ref_seq')),
  access_token_hash      text not null unique,         -- SHA-256 of the secret link token
  customer_name          text not null check (char_length(customer_name) between 2 and 80),
  customer_phone         text not null check (customer_phone ~ '^[6-9][0-9]{9}$'),
  customer_notes         text not null default '' check (char_length(customer_notes) <= 500),
  service_id             uuid not null references public.services(id) on delete restrict,
  service_name           text not null,                -- snapshot at booking time
  price                  integer not null,             -- snapshot at booking time
  duration_minutes       integer not null,             -- snapshot at booking time
  appointment_start      timestamptz not null,
  appointment_end        timestamptz not null,
  status                 text not null default 'pending'
                         check (status in ('pending','confirmed','declined','cancelled','expired')),
  expires_at             timestamptz not null,         -- pending requests lapse here
  telegram_message_id    bigint,
  notification_status    text not null default 'pending' check (notification_status in ('pending','sent','failed')),
  notification_attempts  integer not null default 0,
  notification_error     text,
  idempotency_key        text unique check (char_length(idempotency_key) <= 100),
  client_ip_hash         text,
  decided_by_telegram_id bigint,
  confirmed_at           timestamptz,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now(),
  check (appointment_end > appointment_start),

  -- THE double-booking guard: two CONFIRMED appointments can never overlap.
  -- Pending/declined/cancelled/expired rows are ignored by the constraint.
  constraint bookings_no_overlap_when_confirmed
    exclude using gist (tstzrange(appointment_start, appointment_end, '[)') with &&)
    where (status = 'confirmed')
);

create index bookings_status_start_idx on public.bookings (status, appointment_start);
create index bookings_phone_pending_idx on public.bookings (customer_phone) where status = 'pending';
create index bookings_ip_created_idx   on public.bookings (client_ip_hash, created_at);
create index bookings_notify_idx       on public.bookings (notification_status) where status = 'pending';

-- Remembers processed Telegram update ids so replayed webhooks are ignored.
create table public.telegram_updates (
  update_id   bigint primary key,
  received_at timestamptz not null default now()
);

-- ───────────────────── Status-change guard ─────────────────────
-- Even a buggy API call cannot make an illegal status change.
create function public.guard_booking_update() returns trigger
language plpgsql set search_path = public, pg_temp as $$
begin
  if new.booking_reference is distinct from old.booking_reference
     or new.access_token_hash is distinct from old.access_token_hash then
    raise exception 'immutable_field';
  end if;

  if new.status <> old.status and not (
       (old.status = 'pending'   and new.status in ('confirmed','declined','cancelled','expired'))
    or (old.status = 'confirmed' and new.status = 'cancelled')
  ) then
    raise exception 'invalid_status_transition: % -> %', old.status, new.status;
  end if;

  if old.status <> 'pending' and (
       new.appointment_start <> old.appointment_start
    or new.appointment_end   <> old.appointment_end
    or new.service_id        <> old.service_id) then
    raise exception 'cannot_change_decided_booking';
  end if;

  if new.updated_at is not distinct from old.updated_at then
    new.updated_at := now();
  end if;
  return new;
end $$;

create trigger bookings_guard before update on public.bookings
for each row execute function public.guard_booking_update();

-- ───────────────────── Availability ─────────────────────

-- Is [p_start, p_end) inside opening hours, outside breaks, and on a working day?
create function public.is_within_hours(p_start timestamptz, p_end timestamptz)
returns boolean language plpgsql stable security definer set search_path = public, pg_temp as $$
declare
  v_tz text; v_ls timestamp; v_le timestamp; v_day date; v_dow int; oh public.opening_hours;
begin
  select timezone into v_tz from public.salon_settings where id = 1;
  v_ls := p_start at time zone v_tz;
  v_le := p_end   at time zone v_tz;
  v_day := v_ls::date;
  v_dow := extract(dow from v_day)::int;

  if v_le <= v_ls or v_le::date <> v_day then return false; end if;          -- never crosses midnight
  if exists (select 1 from public.days_off where day = v_day) then return false; end if;

  select * into oh from public.opening_hours where weekday = v_dow and is_open;
  if not found then return false; end if;
  if v_ls::time < oh.open_time or v_le::time > oh.close_time then return false; end if;

  if exists (select 1 from public.breaks b
             where (b.weekday is null or b.weekday = v_dow)
               and v_ls::time < b.end_time and v_le::time > b.start_time) then
    return false;
  end if;
  return true;
end $$;

-- Bookable start times for a service on a local date. Only CONFIRMED bookings block.
create function public.get_available_slots(p_service_id uuid, p_date date)
returns table (slot_start timestamptz, slot_end timestamptz)
language plpgsql stable security definer set search_path = public, pg_temp as $$
declare
  cfg public.salon_settings; v_dur int; oh public.opening_hours;
begin
  select * into cfg from public.salon_settings where id = 1;
  select s.duration_minutes into v_dur from public.services s where s.id = p_service_id and s.is_active;
  if not found then return; end if;

  select * into oh from public.opening_hours where weekday = extract(dow from p_date)::int and is_open;
  if not found then return; end if;

  return query
  select x.s, x.s + make_interval(mins => v_dur)
  from (
    select (g.t at time zone cfg.timezone) as s
    from generate_series(
      (p_date + oh.open_time)::timestamp,
      (p_date + oh.close_time)::timestamp - make_interval(mins => v_dur),
      make_interval(mins => cfg.slot_interval_minutes)
    ) as g(t)
  ) x
  where x.s >= now() + make_interval(mins => cfg.min_advance_minutes)
    and x.s <= now() + make_interval(days => cfg.max_advance_days)
    and public.is_within_hours(x.s, x.s + make_interval(mins => v_dur))
    and not exists (
      select 1 from public.bookings b
      where b.status = 'confirmed'
        and tstzrange(b.appointment_start, b.appointment_end, '[)')
            && tstzrange(x.s, x.s + make_interval(mins => v_dur), '[)'))
  order by x.s;
end $$;

-- ───────────────────── Customer actions ─────────────────────

-- Creates a PENDING request after re-checking everything server-side.
-- Errors (message text): service_not_found, invalid_name, invalid_phone,
-- invalid_notes, slot_unavailable, rate_limited.
create function public.create_booking_request(
  p_service_id uuid, p_start timestamptz, p_name text, p_phone text, p_notes text,
  p_token_hash text, p_idempotency_key text, p_ip_hash text)
returns public.bookings
language plpgsql security definer set search_path = public, pg_temp as $$
declare
  cfg public.salon_settings; svc public.services; v_row public.bookings;
  v_name text := btrim(coalesce(p_name, ''));
  v_phone text := btrim(coalesce(p_phone, ''));
  v_notes text := btrim(coalesce(p_notes, ''));
begin
  select * into cfg from public.salon_settings where id = 1;

  -- A retry of the same form submission returns the original booking.
  if p_idempotency_key is not null then
    select * into v_row from public.bookings where idempotency_key = p_idempotency_key;
    if found then return v_row; end if;
  end if;

  if char_length(v_name) not between 2 and 80 then raise exception 'invalid_name'; end if;
  if v_phone !~ '^[6-9][0-9]{9}$' then raise exception 'invalid_phone'; end if;
  if char_length(v_notes) > 500 then raise exception 'invalid_notes'; end if;

  select * into svc from public.services where id = p_service_id and is_active;
  if not found then raise exception 'service_not_found'; end if;

  -- Serialise requests per phone number so the limit below cannot be raced.
  perform pg_advisory_xact_lock(hashtext('phone:' || v_phone));

  if (select count(*) from public.bookings
      where customer_phone = v_phone and status = 'pending' and expires_at > now())
     >= cfg.max_pending_per_phone then
    raise exception 'rate_limited';
  end if;
  if p_ip_hash is not null and (select count(*) from public.bookings
      where client_ip_hash = p_ip_hash and created_at > now() - interval '1 hour')
     >= cfg.max_requests_per_ip_hour then
    raise exception 'rate_limited';
  end if;

  if not exists (
    select 1 from public.get_available_slots(p_service_id, (p_start at time zone cfg.timezone)::date) g
    where g.slot_start = p_start) then
    raise exception 'slot_unavailable';
  end if;

  begin
    insert into public.bookings (access_token_hash, customer_name, customer_phone, customer_notes,
      service_id, service_name, price, duration_minutes, appointment_start, appointment_end,
      expires_at, idempotency_key, client_ip_hash)
    values (p_token_hash, v_name, v_phone, v_notes,
      svc.id, svc.name, svc.price, svc.duration_minutes, p_start,
      p_start + make_interval(mins => svc.duration_minutes),
      least(now() + make_interval(hours => cfg.pending_expiry_hours), p_start),
      p_idempotency_key, p_ip_hash)
    returning * into v_row;
  exception when unique_violation then
    -- two identical submissions arrived at the same moment
    select * into v_row from public.bookings where idempotency_key = p_idempotency_key;
    if not found then raise; end if;
  end;
  return v_row;
end $$;

-- Customer status lookup. Needs the reference AND the secret token's hash.
create function public.get_booking_status(p_reference text, p_token_hash text)
returns table (booking_reference text, service_name text, price integer, duration_minutes integer,
               appointment_start timestamptz, appointment_end timestamptz, status text)
language plpgsql stable security definer set search_path = public, pg_temp as $$
begin
  return query
  select b.booking_reference, b.service_name, b.price, b.duration_minutes,
         b.appointment_start, b.appointment_end,
         case when b.status = 'pending' and b.expires_at <= now() then 'expired' else b.status end
  from public.bookings b
  where b.booking_reference = p_reference and b.access_token_hash = p_token_hash;
end $$;

-- ───────────────────── Owner actions (Telegram) ─────────────────────
-- Each returns (result, booking_status). Rows are locked first, so a double tap
-- or a stale button cannot change a booking that was already decided.

create function public.confirm_booking(p_booking_id uuid, p_telegram_user_id bigint)
returns table (result text, booking_status text)
language plpgsql security definer set search_path = public, pg_temp as $$
declare b public.bookings;
begin
  select * into b from public.bookings where id = p_booking_id for update;
  if not found then return query select 'not_found'::text, null::text; return; end if;
  if b.status <> 'pending' then return query select 'not_pending'::text, b.status; return; end if;

  if b.expires_at <= now() then
    update public.bookings set status = 'expired' where id = b.id;
    return query select 'expired'::text, 'expired'::text; return;
  end if;
  if b.appointment_start <= now() or not public.is_within_hours(b.appointment_start, b.appointment_end) then
    return query select 'invalid_slot'::text, b.status; return;
  end if;

  begin
    update public.bookings
       set status = 'confirmed', confirmed_at = now(), decided_by_telegram_id = p_telegram_user_id
     where id = b.id;
  exception when exclusion_violation then
    -- another confirmed appointment overlaps: stay pending, owner decides what to do
    return query select 'conflict'::text, 'pending'::text; return;
  end;
  return query select 'confirmed'::text, 'confirmed'::text;
end $$;

create function public.decline_booking(p_booking_id uuid, p_telegram_user_id bigint)
returns table (result text, booking_status text)
language plpgsql security definer set search_path = public, pg_temp as $$
declare b public.bookings;
begin
  select * into b from public.bookings where id = p_booking_id for update;
  if not found then return query select 'not_found'::text, null::text; return; end if;
  if b.status <> 'pending' then return query select 'not_pending'::text, b.status; return; end if;
  update public.bookings set status = 'declined', decided_by_telegram_id = p_telegram_user_id where id = b.id;
  return query select 'declined'::text, 'declined'::text;
end $$;

create function public.cancel_booking(p_booking_id uuid, p_telegram_user_id bigint)
returns table (result text, booking_status text)
language plpgsql security definer set search_path = public, pg_temp as $$
declare b public.bookings;
begin
  select * into b from public.bookings where id = p_booking_id for update;
  if not found then return query select 'not_found'::text, null::text; return; end if;
  if b.status not in ('pending','confirmed') then return query select 'not_cancellable'::text, b.status; return; end if;
  update public.bookings set status = 'cancelled', decided_by_telegram_id = p_telegram_user_id where id = b.id;
  return query select 'cancelled'::text, 'cancelled'::text;
end $$;

create function public.owner_appointments(p_from timestamptz, p_to timestamptz,
                                          p_statuses text[] default array['confirmed'])
returns setof public.bookings
language sql stable security definer set search_path = public, pg_temp as $$
  select * from public.bookings
  where appointment_start >= p_from and appointment_start < p_to and status = any (p_statuses)
  order by appointment_start;
$$;

-- ───────────────────── Housekeeping ─────────────────────

create function public.expire_pending_bookings() returns integer
language plpgsql security definer set search_path = public, pg_temp as $$
declare n integer;
begin
  with u as (update public.bookings set status = 'expired'
             where status = 'pending' and expires_at <= now() returning 1)
  select count(*) into n from u;
  return n;
end $$;

-- True the first time an update id is seen, false for a replay.
create function public.record_telegram_update(p_update_id bigint) returns boolean
language plpgsql security definer set search_path = public, pg_temp as $$
declare n integer;
begin
  insert into public.telegram_updates (update_id) values (p_update_id) on conflict do nothing;
  get diagnostics n = row_count;
  delete from public.telegram_updates where received_at < now() - interval '7 days';
  return n = 1;
end $$;

-- Notification delivery bookkeeping + retry queue.
create function public.mark_notification_result(p_id uuid, p_ok boolean, p_message_id bigint, p_error text)
returns void language sql security definer set search_path = public, pg_temp as $$
  update public.bookings
     set notification_status = case when p_ok then 'sent' else 'failed' end,
         telegram_message_id = coalesce(p_message_id, telegram_message_id),
         notification_error  = case when p_ok then null else left(p_error, 300) end,
         notification_attempts = notification_attempts + 1
   where id = p_id;
$$;

-- Pending bookings whose owner notification has not been delivered (max 10 tries, 2 min apart).
create function public.claim_unsent_notifications(p_limit integer default 20)
returns setof public.bookings
language sql security definer set search_path = public, pg_temp as $$
  update public.bookings set updated_at = now()
   where id in (
     select id from public.bookings
      where status = 'pending' and expires_at > now()
        and notification_status in ('pending','failed')
        and notification_attempts < 10
        and updated_at < now() - interval '2 minutes'
      order by created_at limit p_limit for update skip locked)
  returning *;
$$;

-- ───────────────────── Security ─────────────────────
-- Row Level Security on everything. The browser (anon key) can only read active services.
-- All other access goes through server-side code using the service-role key.

alter table public.services         enable row level security;
alter table public.salon_settings   enable row level security;
alter table public.opening_hours    enable row level security;
alter table public.breaks           enable row level security;
alter table public.days_off         enable row level security;
alter table public.bookings         enable row level security;
alter table public.telegram_updates enable row level security;

create policy "anyone can read active services" on public.services
  for select to anon, authenticated using (is_active);

revoke all on all tables    in schema public from anon, authenticated;
revoke all on all sequences in schema public from anon, authenticated;
grant select on public.services to anon, authenticated;

-- Functions are callable only by the service role.
do $$
declare f record;
begin
  for f in select p.oid::regprocedure as sig
           from pg_proc p join pg_namespace n on n.oid = p.pronamespace
           where n.nspname = 'public' and p.prokind = 'f'
  loop
    execute format('revoke all on function %s from public, anon, authenticated', f.sig);
    execute format('grant execute on function %s to service_role', f.sig);
  end loop;
end $$;
