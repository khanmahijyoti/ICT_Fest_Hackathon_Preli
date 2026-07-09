"""Comprehensive black-box test suite for the CoWork API.

Exercises every endpoint and every business rule from the problem statement
(Sections 3-5), including the concurrency guarantees, against a *running*
server. Run it with the API up (e.g. ``docker compose up``):

    python tests/api_live_test.py [BASE_URL]

BASE_URL defaults to http://localhost:8000. The suite is self-contained and
re-runnable: every run registers fresh organizations/users/rooms (random
suffix), so it never collides with existing data or previous runs.

Covers (numbers = business rules in the problem statement):
  R1  datetime handling (offset -> UTC conversion, naive = UTC, UTC output)
  R2  price, whole-hour duration 1..8, future start, end > start
  R3  no double-booking, back-to-back allowed, concurrent-safe
  R4  member quota 3 in (now, now+24h], admin exempt, concurrent-safe
  R5  rate limit 20/60s per user
  R6  refund tiers 100/50/0, half-cent-up rounding, single RefundLog,
      response == ledger, ALREADY_CANCELLED, concurrent-safe
  R7  reference-code uniqueness (checked across every booking in the run)
  R8  JWT claims, 900s access / 7d refresh expiry, logout revocation,
      single-use refresh tokens
  R9  multi-tenancy isolation on every resource (404 for cross-org ids)
  R10 booking visibility (member: own only; admin: org-wide)
  R11 pagination defaults, ascending order, no skip/repeat, total
  R12 usage report: per-room incl. zero-booking rooms, inclusive range,
      confirmed only, immediately fresh
  R13 availability: per-UTC-date busy intervals, sorted, immediately fresh
  R14 room stats consistent with bookings, incl. after concurrent bursts
  R15 registration: new org -> admin, existing -> member, USERNAME_TAKEN
  R16 liveness: concurrent create+cancel bursts must not hang
"""
import base64
import json
import sys
import threading
import uuid
from datetime import datetime, timedelta

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
TIMEOUT = 30  # generous; also acts as the deadlock detector

# ---------------------------------------------------------------------------
# tiny check framework
# ---------------------------------------------------------------------------
RESULTS = []  # (section, name, ok, detail)
_SECTION = "?"


def section(name):
    global _SECTION
    _SECTION = name
    print(f"\n=== {name} ===")


def check(name, ok, detail=""):
    RESULTS.append((_SECTION, name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if not ok and detail:
        line += f"  -- {detail}"
    print(line)
    return ok


def eq(name, actual, expected):
    return check(name, actual == expected, f"expected {expected!r}, got {actual!r}")


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
RUN = uuid.uuid4().hex[:8]  # unique namespace for this run
ALL_REFERENCE_CODES = []    # every reference_code seen -> global uniqueness check
_ref_lock = threading.Lock()


def api(method, path, token=None, json_body=None, params=None, timeout=TIMEOUT):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.request(
        method, BASE + path, json=json_body, params=params,
        headers=headers, timeout=timeout,
    )


def register(org, username, password="pw12345"):
    return api("POST", "/auth/register",
               json_body={"org_name": org, "username": username, "password": password})


def login(org, username, password="pw12345"):
    return api("POST", "/auth/login",
               json_body={"org_name": org, "username": username, "password": password})


def make_user(org, username, password="pw12345"):
    """Register (ignoring outcome) + login; returns (access_token, register_json)."""
    reg = register(org, username, password)
    tok = login(org, username, password).json()["access_token"]
    return tok, (reg.json() if reg.status_code == 201 else None)


def make_room(admin_token, name, rate_cents=10000, capacity=4):
    r = api("POST", "/rooms", admin_token,
            {"name": name, "capacity": capacity, "hourly_rate_cents": rate_cents})
    assert r.status_code == 201, f"room create failed: {r.status_code} {r.text}"
    return r.json()["id"]


def book(token, room_id, start, end):
    r = api("POST", "/bookings", token,
            {"room_id": room_id, "start_time": start, "end_time": end})
    if r.status_code == 201:
        with _ref_lock:
            ALL_REFERENCE_CODES.append(r.json()["reference_code"])
    return r


def iso(dt):
    """Naive UTC datetime -> ISO string (no offset = treated as UTC by the API)."""
    return dt.replace(microsecond=0).isoformat()


def jwt_claims(token):
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def err_code(resp):
    try:
        return resp.json().get("code")
    except Exception:
        return None


NOW = datetime.utcnow().replace(microsecond=0)


def at(hours, minutes=0):
    """Naive-UTC ISO timestamp `hours` from the suite's start."""
    return iso(NOW + timedelta(hours=hours, minutes=minutes))


# ---------------------------------------------------------------------------
# test sections
# ---------------------------------------------------------------------------
def test_health():
    section("Health")
    r = api("GET", "/health")
    eq("GET /health -> 200", r.status_code, 200)
    eq("health body", r.json(), {"status": "ok"})


def test_registration():
    section("R15 Registration & roles")
    org = f"org-{RUN}-reg"
    r = register(org, "alice")
    eq("new org register -> 201", r.status_code, 201)
    body = r.json()
    eq("first user in new org is admin", body.get("role"), "admin")
    check("register response shape",
          {"user_id", "org_id", "username", "role"} <= set(body),
          f"got keys {sorted(body)}")

    r = register(org, "bob")
    eq("second user joins as member", r.json().get("role"), "member")

    r = register(org, "alice")
    eq("duplicate username -> 409", r.status_code, 409)
    eq("duplicate username code", err_code(r), "USERNAME_TAKEN")

    r = register(f"org-{RUN}-reg2", "alice")
    eq("same username in different org allowed", r.status_code, 201)


def test_auth_tokens():
    section("R8 Login, JWT claims & token lifetimes")
    org = f"org-{RUN}-auth"
    register(org, "alice")

    r = login(org, "alice")
    eq("login -> 200", r.status_code, 200)
    body = r.json()
    check("login shape", {"access_token", "refresh_token", "token_type"} <= set(body))
    eq("token_type", body.get("token_type"), "bearer")

    eq("wrong password -> 401", login(org, "alice", "wrong").status_code, 401)
    eq("wrong password code", err_code(login(org, "alice", "wrong")), "INVALID_CREDENTIALS")
    eq("unknown user -> 401", login(org, "nobody").status_code, 401)
    eq("unknown org -> 401", login(f"org-{RUN}-ghost", "alice").status_code, 401)

    access, refresh = body["access_token"], body["refresh_token"]
    a, rf = jwt_claims(access), jwt_claims(refresh)
    check("access claims present",
          {"sub", "org", "role", "jti", "iat", "exp", "type"} <= set(a),
          f"got {sorted(a)}")
    check("sub is a string", isinstance(a.get("sub"), str), f"sub={a.get('sub')!r}")
    eq("access type claim", a.get("type"), "access")
    eq("refresh type claim", rf.get("type"), "refresh")
    eq("access lifetime exactly 900s", a["exp"] - a["iat"], 900)
    eq("refresh lifetime exactly 7 days", rf["exp"] - rf["iat"], 7 * 24 * 3600)

    b2 = login(org, "alice").json()
    check("jti unique per token",
          len({a["jti"], rf["jti"], jwt_claims(b2["access_token"])["jti"],
               jwt_claims(b2["refresh_token"])["jti"]}) == 4)


def test_refresh_rotation():
    section("R8 Refresh rotation (single-use)")
    org = f"org-{RUN}-refresh"
    register(org, "alice")
    tokens = login(org, "alice").json()

    r = api("POST", "/auth/refresh", json_body={"refresh_token": tokens["refresh_token"]})
    eq("refresh -> 200", r.status_code, 200)
    new = r.json()
    check("refresh returns new token pair",
          {"access_token", "refresh_token", "token_type"} <= set(new))

    r2 = api("POST", "/auth/refresh", json_body={"refresh_token": tokens["refresh_token"]})
    eq("refresh token reuse -> 401", r2.status_code, 401)

    eq("rotated access token works",
       api("GET", "/rooms", new["access_token"]).status_code, 200)
    r3 = api("POST", "/auth/refresh", json_body={"refresh_token": new["refresh_token"]})
    eq("rotated refresh token works once", r3.status_code, 200)

    eq("access token passed as refresh -> 401",
       api("POST", "/auth/refresh",
           json_body={"refresh_token": tokens["access_token"]}).status_code, 401)
    eq("garbage refresh token -> 401",
       api("POST", "/auth/refresh",
           json_body={"refresh_token": "not.a.jwt"}).status_code, 401)


def test_logout():
    section("R8 Logout revocation & auth guards")
    org = f"org-{RUN}-logout"
    register(org, "alice")
    access = login(org, "alice").json()["access_token"]

    eq("token valid before logout", api("GET", "/rooms", access).status_code, 200)
    eq("logout -> 200", api("POST", "/auth/logout", access).status_code, 200)
    eq("token rejected immediately after logout",
       api("GET", "/rooms", access).status_code, 401)

    eq("missing token -> 401", api("GET", "/rooms").status_code, 401)
    eq("malformed token -> 401", api("GET", "/rooms", "garbage").status_code, 401)
    r = requests.get(BASE + "/rooms", headers={"Authorization": "Basic abc"}, timeout=TIMEOUT)
    eq("non-bearer auth scheme -> 401", r.status_code, 401)


def test_rooms_and_tenancy():
    section("R9 Rooms & multi-tenant isolation")
    org_a, org_b = f"org-{RUN}-tenA", f"org-{RUN}-tenB"
    admin_a, _ = make_user(org_a, "admin")
    member_a, _ = make_user(org_a, "member1")
    admin_b, _ = make_user(org_b, "admin")

    r = api("POST", "/rooms", member_a,
            {"name": "nope", "capacity": 2, "hourly_rate_cents": 100})
    eq("member cannot create room -> 403", r.status_code, 403)
    eq("member create room code", err_code(r), "FORBIDDEN")

    r = api("POST", "/rooms", admin_a,
            {"name": "Alpha", "capacity": 4, "hourly_rate_cents": 5000})
    eq("admin creates room -> 201", r.status_code, 201)
    room_a = r.json()
    check("room shape",
          {"id", "org_id", "name", "capacity", "hourly_rate_cents"} <= set(room_a))

    rooms_a = api("GET", "/rooms", admin_a).json()
    check("own org sees its room", any(x["id"] == room_a["id"] for x in rooms_a))
    rooms_b = api("GET", "/rooms", admin_b).json()
    check("other org does not see it", not any(x["id"] == room_a["id"] for x in rooms_b))

    for path, what in [
        (f"/rooms/{room_a['id']}/availability?date=2030-01-01", "availability"),
        (f"/rooms/{room_a['id']}/stats", "stats"),
    ]:
        r = api("GET", path, admin_b)
        eq(f"cross-org room {what} -> 404", r.status_code, 404)
        eq(f"cross-org room {what} code", err_code(r), "ROOM_NOT_FOUND")

    r = book(admin_b, room_a["id"], at(50), at(51))
    eq("booking a cross-org room -> 404 ROOM_NOT_FOUND",
       (r.status_code, err_code(r)), (404, "ROOM_NOT_FOUND"))


def test_booking_validation():
    section("R2 Booking window, duration & price")
    org = f"org-{RUN}-val"
    admin, _ = make_user(org, "admin")
    room = make_room(admin, "Val", rate_cents=2500)

    cases = [
        ("start in the past", at(-2), at(-1)),
        ("start exactly now-ish", iso(NOW - timedelta(seconds=5)), at(1)),
        ("end == start", at(3), at(3)),
        ("end before start", at(4), at(3)),
        ("30-minute duration", at(5), at(5, 30)),
        ("90-minute (non-whole) duration", at(6), at(7, 30)),
        ("9-hour duration", at(10), at(19)),
    ]
    for name, s, e in cases:
        r = book(admin, room, s, e)
        eq(f"{name} -> 400 INVALID_BOOKING_WINDOW",
           (r.status_code, err_code(r)), (400, "INVALID_BOOKING_WINDOW"))

    r = book(admin, room, at(20), at(21))
    eq("1-hour booking -> 201", r.status_code, 201)
    b = r.json()
    check("booking shape",
          {"id", "reference_code", "room_id", "user_id", "start_time", "end_time",
           "status", "price_cents", "created_at"} <= set(b), f"got {sorted(b)}")
    eq("status confirmed", b["status"], "confirmed")
    eq("price = rate x 1h", b["price_cents"], 2500)
    check("response datetimes carry UTC designator",
          all(b[k].endswith("+00:00") or b[k].endswith("Z")
              for k in ("start_time", "end_time", "created_at")),
          f"start={b['start_time']} end={b['end_time']} created={b['created_at']}")

    r = book(admin, room, at(30), at(38))
    eq("8-hour booking -> 201", r.status_code, 201)
    eq("price = rate x 8h", r.json()["price_cents"], 2500 * 8)

    r = book(admin, room, iso(NOW + timedelta(days=200)), iso(NOW + timedelta(days=200, hours=1)))
    eq("far-future booking allowed", r.status_code, 201)

    r = book(admin, 99999999, at(40), at(41))
    eq("unknown room -> 404 ROOM_NOT_FOUND",
       (r.status_code, err_code(r)), (404, "ROOM_NOT_FOUND"))

    # Malformed datetimes must be rejected cleanly, never 500 (liveness).
    for name, s, e in [
        ("garbage start_time", "banana", at(41)),
        ("garbage end_time", at(40), "not-a-date"),
        ("empty start_time", "", at(41)),
    ]:
        r = book(admin, room, s, e)
        eq(f"{name} -> 400 INVALID_BOOKING_WINDOW",
           (r.status_code, err_code(r)), (400, "INVALID_BOOKING_WINDOW"))


def test_timezone_handling():
    section("R1 Offset datetimes are converted to UTC")
    org = f"org-{RUN}-tz"
    admin, _ = make_user(org, "admin")
    room = make_room(admin, "TZ")

    # 10:00+06:00 == 04:00 UTC. Pick a fixed slot 3 days out.
    base = (NOW + timedelta(days=3)).replace(hour=10, minute=0, second=0)
    r = book(admin, room, base.isoformat() + "+06:00",
             (base + timedelta(hours=1)).isoformat() + "+06:00")
    eq("offset booking accepted", r.status_code, 201)
    utc_start = base - timedelta(hours=6)
    got = r.json()["start_time"]
    check("start_time stored/echoed as UTC",
          got.startswith(utc_start.isoformat()), f"expected {utc_start} UTC, got {got}")

    # The same wall-clock instant expressed naively (UTC) must now conflict.
    r = book(admin, room, iso(utc_start), iso(utc_start + timedelta(hours=1)))
    eq("naive-UTC twin of offset slot conflicts",
       (r.status_code, err_code(r)), (409, "ROOM_CONFLICT"))

    # A different offset spelling of the *same* instant must also conflict.
    r = book(admin, room, (utc_start + timedelta(hours=1)).isoformat() + "+01:00",
             (utc_start + timedelta(hours=2)).isoformat() + "+01:00")
    eq("+01:00 twin of same instant conflicts", r.status_code, 409)


def test_overlap():
    section("R3 Overlap semantics")
    org = f"org-{RUN}-ovl"
    admin, _ = make_user(org, "admin")
    room = make_room(admin, "Ovl")
    room2 = make_room(admin, "Ovl2")

    day = NOW + timedelta(days=2)
    s = lambda h: iso(day.replace(hour=h, minute=0, second=0))

    eq("seed booking 10:00-12:00", book(admin, room, s(10), s(12)).status_code, 201)
    for name, a, b in [
        ("identical interval", s(10), s(12)),
        ("overlap at tail (11-13)", s(11), s(13)),
        ("overlap at head (9-11)", s(9), s(11)),
        ("contained (10-11)", s(10), s(11)),
        ("containing (9-13)", s(9), s(13)),
    ]:
        r = book(admin, room, a, b)
        eq(f"{name} -> 409 ROOM_CONFLICT",
           (r.status_code, err_code(r)), (409, "ROOM_CONFLICT"))

    eq("back-to-back after (12-13) allowed", book(admin, room, s(12), s(13)).status_code, 201)
    eq("back-to-back before (9-10) allowed", book(admin, room, s(9), s(10)).status_code, 201)
    eq("same slot, different room allowed", book(admin, room2, s(10), s(12)).status_code, 201)

    r = book(admin, room, s(10), s(12))
    eq("cancelled slot frees up: still 409 while confirmed", r.status_code, 409)


def test_quota():
    section("R4 Member quota: 3 confirmed in (now, now+24h]")
    org = f"org-{RUN}-quota"
    admin, _ = make_user(org, "admin")
    member, _ = make_user(org, "member1")
    room = make_room(admin, "Q")

    ids = []
    for h in (2, 4, 6):
        r = book(member, room, at(h), at(h + 1))
        eq(f"member booking {h}h out -> 201", r.status_code, 201)
        ids.append(r.json()["id"])

    r = book(member, room, at(8), at(9))
    eq("4th within 24h -> 409 QUOTA_EXCEEDED",
       (r.status_code, err_code(r)), (409, "QUOTA_EXCEEDED"))

    r = book(member, room, at(30), at(31))
    eq("booking outside 24h window unaffected by quota", r.status_code, 201)

    r = api("POST", f"/bookings/{ids[0]}/cancel", member)
    eq("cancel one in-window booking", r.status_code, 200)
    r = book(member, room, at(8), at(9))
    eq("cancelled bookings do not count toward quota", r.status_code, 201)

    for h in (10, 12, 14, 16):
        r = book(admin, room, at(h), at(h + 1))
        eq(f"admin exempt from quota ({h}h out)", r.status_code, 201)


def test_pagination():
    section("R11 Pagination & ordering")
    org = f"org-{RUN}-page"
    admin, _ = make_user(org, "admin")
    member, _ = make_user(org, "pager")
    room = make_room(admin, "P")

    # Create 7 bookings outside the quota window, in shuffled order.
    hours = [31, 27, 35, 25, 33, 29, 37]
    for h in hours:
        r = book(member, room, at(h), at(h + 1))
        assert r.status_code == 201, r.text

    r = api("GET", "/bookings", member)
    eq("defaults: page=1 limit=10", (r.json()["page"], r.json()["limit"]), (1, 10))
    eq("total = 7", r.json()["total"], 7)
    starts = [b["start_time"] for b in r.json()["items"]]
    eq("sorted ascending by start_time", starts, sorted(starts))

    seen = []
    for page in (1, 2, 3):
        r = api("GET", "/bookings", member, params={"page": page, "limit": 3})
        items = r.json()["items"]
        check(f"page {page} size", len(items) == (3 if page < 3 else 1),
              f"got {len(items)}")
        seen += [b["id"] for b in items]
    eq("pages neither skip nor repeat", len(set(seen)), 7)
    all_ids = [b["id"] for b in api("GET", "/bookings", member).json()["items"]]
    eq("paged union == full listing", seen, all_ids)

    eq("limit above max -> 422",
       api("GET", "/bookings", member, params={"limit": 101}).status_code, 422)
    eq("page 0 -> 422",
       api("GET", "/bookings", member, params={"page": 0}).status_code, 422)
    eq("admin listing shows only own bookings",
       api("GET", "/bookings", admin).json()["total"], 0)


def test_visibility():
    section("R10 Booking visibility")
    org = f"org-{RUN}-vis"
    admin, _ = make_user(org, "admin")
    m1, _ = make_user(org, "m1")
    m2, _ = make_user(org, "m2")
    other_admin, _ = make_user(f"org-{RUN}-vis2", "admin")
    room = make_room(admin, "V")

    bid = book(m1, room, at(28), at(29)).json()["id"]

    eq("owner reads own booking", api("GET", f"/bookings/{bid}", m1).status_code, 200)
    r = api("GET", f"/bookings/{bid}", m2)
    eq("other member -> 404 BOOKING_NOT_FOUND",
       (r.status_code, err_code(r)), (404, "BOOKING_NOT_FOUND"))
    r = api("POST", f"/bookings/{bid}/cancel", m2)
    eq("other member cannot cancel -> 404",
       (r.status_code, err_code(r)), (404, "BOOKING_NOT_FOUND"))
    eq("same-org admin reads member booking",
       api("GET", f"/bookings/{bid}", admin).status_code, 200)
    eq("cross-org admin -> 404",
       api("GET", f"/bookings/{bid}", other_admin).status_code, 404)
    eq("cross-org admin cannot cancel -> 404",
       api("POST", f"/bookings/{bid}/cancel", other_admin).status_code, 404)
    eq("unknown booking id -> 404",
       api("GET", "/bookings/99999999", m1).status_code, 404)

    eq("same-org admin can cancel member booking",
       api("POST", f"/bookings/{bid}/cancel", admin).status_code, 200)


def test_refunds():
    section("R6 Cancellation refund policy")
    org = f"org-{RUN}-ref"
    admin, _ = make_user(org, "admin")
    # Rate 101 -> 50% of 101 = 50.5 -> must round *up* to 51.
    room = make_room(admin, "R", rate_cents=101)

    def cancel_at(hours_out):
        bid = book(admin, room, at(hours_out), at(hours_out + 1)).json()["id"]
        return bid, api("POST", f"/bookings/{bid}/cancel", admin)

    bid, r = cancel_at(72)
    eq("notice >= 48h -> 100%", r.json().get("refund_percent"), 100)
    eq("100% of 101 = 101", r.json().get("refund_amount_cents"), 101)
    check("cancel response shape",
          {"id", "status", "refund_percent", "refund_amount_cents"} <= set(r.json()))
    eq("cancel response status", r.json().get("status"), "cancelled")

    bid50, r = cancel_at(30)
    eq("24h <= notice < 48h -> 50%", r.json().get("refund_percent"), 50)
    eq("half-cent rounds UP: 50% of 101 = 51", r.json().get("refund_amount_cents"), 51)

    _, r = cancel_at(2)
    eq("notice < 24h -> 0%", r.json().get("refund_percent"), 0)
    eq("0% amount = 0", r.json().get("refund_amount_cents"), 0)

    detail = api("GET", f"/bookings/{bid50}", admin).json()
    refunds = detail.get("refunds", [])
    eq("exactly one RefundLog entry", len(refunds), 1)
    eq("ledger amount == cancel response amount", refunds[0]["amount_cents"], 51)
    check("refund entry shape",
          {"amount_cents", "status", "processed_at"} <= set(refunds[0]))
    eq("booking status now cancelled", detail["status"], "cancelled")

    r = api("POST", f"/bookings/{bid50}/cancel", admin)
    eq("re-cancel -> 409 ALREADY_CANCELLED",
       (r.status_code, err_code(r)), (409, "ALREADY_CANCELLED"))


def test_availability():
    section("R13 Availability")
    org = f"org-{RUN}-avail"
    admin, _ = make_user(org, "admin")
    room = make_room(admin, "A")

    day1 = (NOW + timedelta(days=4)).replace(hour=0, minute=0, second=0)
    day2 = day1 + timedelta(days=1)
    d1, d2 = day1.date().isoformat(), day2.date().isoformat()

    # Two bookings on day1 (created out of order) + one on day2.
    b_late = book(admin, room, iso(day1.replace(hour=14)), iso(day1.replace(hour=15)))
    b_early = book(admin, room, iso(day1.replace(hour=9)), iso(day1.replace(hour=10)))
    book(admin, room, iso(day2.replace(hour=9)), iso(day2.replace(hour=10)))
    assert b_late.status_code == b_early.status_code == 201

    r = api("GET", f"/rooms/{room}/availability", admin, params={"date": d1})
    eq("availability -> 200", r.status_code, 200)
    body = r.json()
    eq("availability shape keys", set(body), {"room_id", "date", "busy"})
    eq("only that date's bookings", len(body["busy"]), 2)
    starts = [x["start_time"] for x in body["busy"]]
    eq("busy sorted ascending", starts, sorted(starts))
    check("day2 booking excluded from day1", all(d1 in s for s in starts))

    # Freshness: a new booking must appear immediately (cache invalidation).
    book(admin, room, iso(day1.replace(hour=11)), iso(day1.replace(hour=12)))
    r = api("GET", f"/rooms/{room}/availability", admin, params={"date": d1})
    eq("new booking visible immediately", len(r.json()["busy"]), 3)

    # Freshness: cancelling must remove the interval immediately.
    api("POST", f"/bookings/{b_early.json()['id']}/cancel", admin)
    r = api("GET", f"/rooms/{room}/availability", admin, params={"date": d1})
    eq("cancelled booking removed immediately", len(r.json()["busy"]), 2)

    eq("unknown room availability -> 404",
       api("GET", "/rooms/99999999/availability", admin,
           params={"date": d1}).status_code, 404)


def test_stats():
    section("R14 Room stats")
    org = f"org-{RUN}-stats"
    admin, _ = make_user(org, "admin")
    room = make_room(admin, "S", rate_cents=1000)

    r = api("GET", f"/rooms/{room}/stats", admin)
    eq("fresh room stats zero",
       (r.json()["total_confirmed_bookings"], r.json()["total_revenue_cents"]), (0, 0))

    ids = [book(admin, room, at(40 + 2 * i), at(41 + 2 * i)).json()["id"] for i in range(3)]
    r = api("GET", f"/rooms/{room}/stats", admin).json()
    eq("stats after 3 creates", (r["total_confirmed_bookings"], r["total_revenue_cents"]),
       (3, 3000))

    api("POST", f"/bookings/{ids[0]}/cancel", admin)
    r = api("GET", f"/rooms/{room}/stats", admin).json()
    eq("stats after 1 cancel", (r["total_confirmed_bookings"], r["total_revenue_cents"]),
       (2, 2000))
    eq("stats shape", set(api("GET", f"/rooms/{room}/stats", admin).json()),
       {"room_id", "total_confirmed_bookings", "total_revenue_cents"})


def test_usage_report():
    section("R12 Usage report")
    org = f"org-{RUN}-rpt"
    admin, _ = make_user(org, "admin")
    member, _ = make_user(org, "m1")
    room = make_room(admin, "Rpt", rate_cents=1000)
    empty_room = make_room(admin, "Empty", rate_cents=500)

    eq("member usage-report -> 403",
       (api("GET", "/admin/usage-report", member,
            params={"from": "2030-01-01", "to": "2030-01-02"}).status_code), 403)
    eq("member export -> 403",
       api("GET", "/admin/export", member).status_code, 403)

    day = (NOW + timedelta(days=5)).replace(hour=0, minute=0, second=0)
    d = day.date().isoformat()
    day_after = (day + timedelta(days=1)).date().isoformat()
    day_before = (day - timedelta(days=1)).date().isoformat()

    # One booking at 10:00 and one at 23:00 on `d` (23:00 checks the
    # inclusive upper bound), plus one the day after (must be excluded).
    book(admin, room, iso(day.replace(hour=10)), iso(day.replace(hour=11)))
    book(admin, room, iso(day.replace(hour=23)), iso(day.replace(hour=23) + timedelta(hours=1)))
    excl = book(admin, room, iso(day + timedelta(days=1, hours=10)),
                iso(day + timedelta(days=1, hours=11)))
    assert excl.status_code == 201

    r = api("GET", "/admin/usage-report", admin, params={"from": d, "to": d})
    eq("usage report -> 200", r.status_code, 200)
    body = r.json()
    eq("report echoes range", (body["from"], body["to"]), (d, d))
    rows = {row["room_id"]: row for row in body["rooms"]}
    check("zero-booking room included", empty_room in rows)
    eq("zero-booking room counts", (rows[empty_room]["confirmed_bookings"],
                                    rows[empty_room]["revenue_cents"]), (0, 0))
    eq("range is inclusive of `to` date (both bookings on d counted)",
       (rows[room]["confirmed_bookings"], rows[room]["revenue_cents"]), (2, 2000))
    check("row shape", {"room_id", "room_name", "confirmed_bookings",
                        "revenue_cents"} <= set(rows[room]))

    r = api("GET", "/admin/usage-report", admin, params={"from": day_before, "to": day_after})
    rows = {row["room_id"]: row for row in r.json()["rooms"]}
    eq("wider range counts day-after booking too", rows[room]["confirmed_bookings"], 3)

    # Freshness: cache must be invalidated by create and cancel.
    b = book(admin, room, iso(day.replace(hour=15)), iso(day.replace(hour=16)))
    r = api("GET", "/admin/usage-report", admin, params={"from": d, "to": d})
    rows = {row["room_id"]: row for row in r.json()["rooms"]}
    eq("report reflects new booking immediately", rows[room]["confirmed_bookings"], 3)

    api("POST", f"/bookings/{b.json()['id']}/cancel", admin)
    r = api("GET", "/admin/usage-report", admin, params={"from": d, "to": d})
    rows = {row["room_id"]: row for row in r.json()["rooms"]}
    eq("report drops cancelled booking immediately", rows[room]["confirmed_bookings"], 2)

    # Fresh rooms must appear in an already-cached range.
    new_room = make_room(admin, "Late", rate_cents=700)
    r = api("GET", "/admin/usage-report", admin, params={"from": d, "to": d})
    check("newly created room appears in cached range",
          any(row["room_id"] == new_room for row in r.json()["rooms"]))

    # Cross-org isolation: another org's report must not contain these rooms.
    other_admin, _ = make_user(f"org-{RUN}-rpt2", "admin")
    r = api("GET", "/admin/usage-report", other_admin, params={"from": d, "to": d})
    eq("other org's report has none of our rooms", r.json()["rooms"], [])


def test_export():
    section("R9 Export CSV")
    org = f"org-{RUN}-exp"
    admin, _ = make_user(org, "admin")
    member, _ = make_user(org, "m1")
    room = make_room(admin, "E")
    other_admin, _ = make_user(f"org-{RUN}-exp2", "admin")
    other_room = make_room(other_admin, "OtherRoom")

    admin_bid = book(admin, room, at(44), at(45)).json()["id"]
    member_bid = book(member, room, at(46), at(47)).json()["id"]

    r = api("GET", "/admin/export", admin)
    eq("export -> 200", r.status_code, 200)
    lines = [ln for ln in r.text.strip().splitlines() if ln]
    eq("exact CSV header", lines[0],
       "id,reference_code,room_id,user_id,start_time,end_time,status,price_cents")
    ids_in = {int(ln.split(",")[0]) for ln in lines[1:]}
    check("default export = caller's own bookings only",
          admin_bid in ids_in and member_bid not in ids_in,
          f"rows for ids {sorted(ids_in)}")

    r = api("GET", "/admin/export", admin, params={"include_all": "true"})
    ids_in = {int(ln.split(",")[0]) for ln in r.text.strip().splitlines()[1:]}
    check("include_all covers whole org", {admin_bid, member_bid} <= ids_in)

    r = api("GET", "/admin/export", admin,
            params={"include_all": "true", "room_id": other_room})
    eq("cross-org room export -> 404 ROOM_NOT_FOUND",
       (r.status_code, err_code(r)), (404, "ROOM_NOT_FOUND"))

    r = api("GET", "/admin/export", other_admin, params={"include_all": "true"})
    other_ids = {int(ln.split(",")[0]) for ln in r.text.strip().splitlines()[1:]}
    check("other org's export contains none of ours",
          not ({admin_bid, member_bid} & other_ids))


def test_rate_limit():
    section("R5 Rate limit: 20 booking POSTs / 60s per user")
    org = f"org-{RUN}-rate"
    user, _ = make_user(org, "spammer")

    # Invalid-window bookings still count (all requests count) and fail fast.
    codes = []
    for _ in range(25):
        r = api("POST", "/bookings", user,
                {"room_id": 1, "start_time": at(-2), "end_time": at(-1)})
        codes.append(r.status_code)
    eq("first 20 requests pass the limiter", codes[:20], [400] * 20)
    eq("requests 21-25 -> 429", codes[20:], [429] * 5)
    r = api("POST", "/bookings", user,
            {"room_id": 1, "start_time": at(-2), "end_time": at(-1)})
    eq("429 responses carry RATE_LIMITED code", err_code(r), "RATE_LIMITED")


def run_threads(n, fn):
    """Run fn(i) in n threads; returns results list. Detects hangs via join timeout."""
    results = [None] * n
    def wrap(i):
        try:
            results[i] = fn(i)
        except Exception as e:  # noqa: BLE001 - report, don't kill the suite
            results[i] = e
    threads = [threading.Thread(target=wrap, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=TIMEOUT * 2)
    hung = any(t.is_alive() for t in threads)
    return results, hung


def test_concurrency():
    section("R3/R4/R6/R7/R15/R16 Concurrency")
    org = f"org-{RUN}-conc"
    admin, _ = make_user(org, "admin")

    # --- same-slot booking race (R3) ---
    room = make_room(admin, "C1")
    s, e = at(60), at(61)
    res, hung = run_threads(6, lambda i: book(admin, room, s, e))
    check("no hang during booking race", not hung)
    codes = sorted(r.status_code for r in res)
    eq("same-slot race: exactly one 201", codes, [201, 409, 409, 409, 409, 409])
    check("losers get ROOM_CONFLICT",
          all(err_code(r) == "ROOM_CONFLICT" for r in res if r.status_code == 409))

    # --- quota race (R4): 6 concurrent distinct in-window slots, only 3 may win ---
    member, _ = make_user(org, "racer")
    room_q = make_room(admin, "C2")
    res, hung = run_threads(
        6, lambda i: book(member, room_q, at(2 + i), at(3 + i)))
    check("no hang during quota race", not hung)
    codes = sorted(r.status_code for r in res)
    eq("quota race: exactly three 201", codes, [201, 201, 201, 409, 409, 409])
    check("quota losers get QUOTA_EXCEEDED",
          all(err_code(r) == "QUOTA_EXCEEDED" for r in res if r.status_code == 409))

    # --- concurrent cancel (R6): one winner, one RefundLog ---
    room_c = make_room(admin, "C3", rate_cents=333)
    bid = book(admin, room_c, at(72), at(73)).json()["id"]
    res, hung = run_threads(6, lambda i: api("POST", f"/bookings/{bid}/cancel", admin))
    check("no hang during cancel race", not hung)
    codes = sorted(r.status_code for r in res)
    eq("cancel race: exactly one 200", codes, [200, 409, 409, 409, 409, 409])
    winner = next(r for r in res if r.status_code == 200)
    refunds = api("GET", f"/bookings/{bid}", admin).json()["refunds"]
    eq("exactly one RefundLog after race", len(refunds), 1)
    eq("ledger == winning response",
       refunds[0]["amount_cents"], winner.json()["refund_amount_cents"])

    # --- registration race (R15): one 201, rest 409, zero 5xx ---
    res, hung = run_threads(8, lambda i: register(f"org-{RUN}-regrace", "samename"))
    check("no hang during register race", not hung)
    codes = sorted(r.status_code for r in res)
    eq("register race: one 201, seven 409", codes, [201] + [409] * 7)

    # --- mixed create+cancel burst: liveness / deadlock check (R16) ---
    room_m = make_room(admin, "C4", rate_cents=100)
    seed = [book(admin, room_m, at(80 + 2 * i), at(81 + 2 * i)).json()["id"]
            for i in range(3)]
    def mixed(i):
        if i < 3:
            return api("POST", f"/bookings/{seed[i]}/cancel", admin)
        return book(admin, room_m, at(90 + 2 * i), at(91 + 2 * i))
    res, hung = run_threads(6, mixed)
    check("mixed create+cancel burst completes (no deadlock)", not hung)
    check("burst: all responses well-formed",
          all(getattr(r, "status_code", 0) in (200, 201) for r in res),
          f"codes: {[getattr(r, 'status_code', r) for r in res]}")

    # --- stats consistent after the burst (R14) ---
    st = api("GET", f"/rooms/{room_m}/stats", admin).json()
    eq("stats exact after concurrent burst (3 created+cancelled, 3 live)",
       (st["total_confirmed_bookings"], st["total_revenue_cents"]), (3, 300))


def test_reference_uniqueness():
    section("R7 Reference-code uniqueness (whole run)")
    check(f"all {len(ALL_REFERENCE_CODES)} reference codes unique",
          len(set(ALL_REFERENCE_CODES)) == len(ALL_REFERENCE_CODES),
          "duplicates: %s" % {c for c in ALL_REFERENCE_CODES
                              if ALL_REFERENCE_CODES.count(c) > 1})


# ---------------------------------------------------------------------------
def main():
    print(f"CoWork API live test suite -> {BASE}  (run id {RUN})")
    tests = [
        test_health,
        test_registration,
        test_auth_tokens,
        test_refresh_rotation,
        test_logout,
        test_rooms_and_tenancy,
        test_booking_validation,
        test_timezone_handling,
        test_overlap,
        test_quota,
        test_pagination,
        test_visibility,
        test_refunds,
        test_availability,
        test_stats,
        test_usage_report,
        test_export,
        test_rate_limit,
        test_concurrency,
        test_reference_uniqueness,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:  # a crashed section shouldn't hide the rest
            check(f"{t.__name__} completed without error", False, repr(e))

    failed = [(s, n, d) for s, n, ok, d in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(f"TOTAL: {len(RESULTS)} checks, {len(RESULTS) - len(failed)} passed, "
          f"{len(failed)} failed")
    for s, n, d in failed:
        print(f"  FAIL [{s}] {n}  {('-- ' + d) if d else ''}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
