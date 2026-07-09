# Bug Report — CoWork: Multi-Tenant Coworking Space Booking API

Line numbers refer to the original (unfixed) code at the initial commit. Bugs are grouped by area. Each entry states where the bug is, what it was and why it produced wrong behavior, and how it was fixed.

---

## Authentication & Tokens

### 1. Access tokens expired in 900 minutes instead of 900 seconds
- **File/line:** `app/auth.py:50` (`create_access_token`)
- **Bug:** `lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)`. The config value is already in minutes (15), so multiplying by 60 produced a 900-**minute** (54,000 s) lifetime instead of the required exactly 900 seconds (Rule 8).
- **Fix:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` → `exp - iat == 900`.

### 2. Logout never actually revoked the token
- **File/line:** `app/auth.py:97` (`get_token_payload`), with `revoke_access_token` at `app/auth.py:85-86`
- **Bug:** Logout stored the token's `jti` in `_revoked_tokens`, but the auth dependency checked `payload.get("sub") in _revoked_tokens` — comparing a user id against a set of jtis. The check could never match, so a logged-out access token kept working (Rule 8 requires immediate invalidation → 401).
- **Fix:** Check `payload.get("jti") in _revoked_tokens`.

### 3. Refresh tokens were not single-use
- **File/line:** `app/routers/auth.py:81-93` (`refresh`), `app/auth.py`
- **Bug:** `/auth/refresh` decoded and honored the same refresh token any number of times. Rule 8 requires refresh tokens to be single-use (reuse → 401).
- **Fix:** Added a `_used_refresh_tokens` jti set in `app/auth.py` with `consume_refresh_token()`, called from the refresh endpoint. First use succeeds and records the jti; any replay raises 401.

### 4. Duplicate username returned success instead of 409 USERNAME_TAKEN
- **File/line:** `app/routers/auth.py:37-43` (`register`)
- **Bug:** Registering an existing username in an org returned HTTP 201 with the *existing* user's data (silently "logging in" as someone else's account identity) instead of the contractual `409 USERNAME_TAKEN` (Rule 15).
- **Fix:** Raise `AppError(409, "USERNAME_TAKEN", ...)` when the username already exists in the org.

### 5. Concurrent registration raced into a 500 IntegrityError
- **File/line:** `app/routers/auth.py` (`register`, both commit sites)
- **Bug:** Two concurrent registrations with the same username (or same new org name) could both pass the existence check, then one would hit the DB unique constraint and surface as an unhandled 500. Rules 15/16 imply valid requests must produce contractual responses, never crashes.
- **Fix:** Wrapped both commits in `try/except IntegrityError` with rollback: username race → `409 USERNAME_TAKEN`; org-name race → re-fetch the winner's org and join it as member. Verified: 8 concurrent same-username registers produce exactly one 201 and seven 409s, no 500s.

---

## Booking Creation

### 6. Overlap check rejected valid back-to-back bookings
- **File/line:** `app/routers/bookings.py:50` (`_has_conflict`)
- **Bug:** Used `b.start_time <= end and start <= b.end_time` (inclusive). Rule 3 defines overlap as `existing.start < new.end AND new.start < existing.end` and explicitly allows back-to-back bookings. The inclusive comparison returned `409 ROOM_CONFLICT` for a booking starting exactly when another ends.
- **Fix:** Changed both comparisons to strict `<`.

### 7. 5-minute grace window on start_time
- **File/line:** `app/routers/bookings.py:86` (`create_booking`)
- **Bug:** `if start <= now - timedelta(seconds=300)` accepted bookings starting up to 5 minutes in the past. Rule 2: start must be strictly in the future, **no grace window**.
- **Fix:** `if start <= now`.

### 8. Minimum duration never enforced
- **File/line:** `app/routers/bookings.py:93` (`create_booking`)
- **Bug:** Only `duration_hours > MAX_DURATION_HOURS` was checked. `MIN_DURATION_HOURS` was defined but unused, so 0-hour and negative-duration bookings (end ≤ start) passed validation. Rule 2: minimum 1 hour, end strictly after start.
- **Fix:** `if duration_hours < MIN_DURATION_HOURS or duration_hours > MAX_DURATION_HOURS` → `400 INVALID_BOOKING_WINDOW` (also covers end ≤ start, since duration ≤ 0 < 1).

### 9. Double-booking and quota races under concurrency
- **File/line:** `app/routers/bookings.py:100-118` (`create_booking`), with sleeps `_pricing_warmup`/`_quota_audit` deliberately widening the window
- **Bug:** The conflict check, quota check, and insert were not synchronized. Two concurrent requests for the same slot would both see "no conflict" and both insert (violating Rule 3); likewise two concurrent requests could both pass the quota count and exceed 3 (violating Rule 4).
- **Fix:** Wrapped check-then-insert in a module-level `threading.Lock` (`_creation_lock`) so the conflict check, quota check, and commit are atomic with respect to other creations. The artificial sleeps were hoisted out of the helpers and run *before* the lock, so the lock is held only for the fast check+insert (keeps Rule 16 liveness). Verified: 6 concurrent identical requests → exactly one 201, five 409s.

### 10. Quota applied to admins
- **File/line:** `app/routers/bookings.py:103` (`create_booking` → `_check_quota`)
- **Bug:** The quota was enforced for all users. Rule 4 explicitly scopes the 3-booking / 24-hour quota to **members** ("A member may hold at most 3..."), and the spec distinguishes member vs admin deliberately elsewhere (Rules 6, 10).
- **Fix:** The quota check now runs only when `user.role == "member"`. Verified: member gets 409 on the 4th within-24h booking; admin can create 4+.

### 11. Duplicate reference codes under concurrent creation
- **File/line:** `app/services/reference.py:17-21` (`next_reference_code`)
- **Bug:** Read-sleep-write on a shared counter: two concurrent requests both read `current`, sleep in `_format_pause()`, and both write `current + 1` — issuing the same code twice. Rule 7 requires uniqueness including under concurrent creation.
- **Fix:** Guarded the read-increment with a `threading.Lock`; the formatting sleep was moved outside the lock. Verified: 6 concurrent creations → 6 unique codes.

### 12. Rate limiter lost counts under concurrency
- **File/line:** `app/services/ratelimit.py:18-26` (`record_and_check`)
- **Bug:** Same read-sleep-write race on `_buckets[user_id]`: concurrent requests each read the old bucket, slept in `_settle_pause()`, and wrote back independently — losing entries, so more than 20 requests/60 s could pass (Rule 5).
- **Fix:** The trim-append-check sequence is now under a `threading.Lock`; the sleep runs before the lock.

---

## Booking Read / Listing

### 13. Pagination broken three ways
- **File/line:** `app/routers/bookings.py:134-141` (`list_bookings`)
- **Bug:**
  1. Sorted `start_time.desc()` — Rule 11 requires ascending.
  2. `offset(page * limit)` — page 1 skipped the first `limit` items entirely (off-by-one; must be `(page-1)*limit`), so sequential pages skipped items.
  3. `.limit(10)` hardcoded — the caller's `limit` parameter was ignored.
- **Fix:** `order_by(start_time.asc(), id.asc()).offset((page - 1) * limit).limit(limit)`.

### 14. Members could read other members' bookings
- **File/line:** `app/routers/bookings.py:150-163` (`get_booking`)
- **Bug:** The query only scoped by org, with no ownership check — any member could read any booking in their org via `GET /bookings/{id}`. Rule 10: members may read only their own bookings; another member's id → `404 BOOKING_NOT_FOUND`.
- **Fix:** Added `if user.role != "admin" and booking.user_id != user.id: raise 404 BOOKING_NOT_FOUND` (admins retain org-wide read).

### 15. Booking detail returned created_at as start_time
- **File/line:** `app/routers/bookings.py:166` (`get_booking`)
- **Bug:** `response["start_time"] = iso_utc(booking.created_at)` overwrote the correct serialized start time with the creation timestamp, so `GET /bookings/{id}` reported a wrong `start_time`.
- **Fix:** Removed the overwrite; `serialize_booking` already sets the correct value.

---

## Cancellation & Refunds

### 16. Refund tiers wrong at both boundaries
- **File/line:** `app/routers/bookings.py:199-206` (`cancel_booking`)
- **Bug:** Two errors (Rule 6):
  1. `notice_hours > 48` — truncated integer hours and used strict `>`, so notice of exactly 48h (and anything in [48h, 49h)) fell into the 50% tier instead of 100%.
  2. The final `else` branch returned **50%** for notice < 24h instead of **0%** — every last-minute cancellation was over-refunded.
- **Fix:** Compare the raw timedelta: `notice >= timedelta(hours=48)` → 100, `notice >= timedelta(hours=24)` → 50, else 0.

### 17. Refund amount rounding wrong, and response ≠ RefundLog
- **File/line:** `app/services/refunds.py:14-17` (`log_refund`), `app/routers/bookings.py:208`
- **Bug:** The ledger computed the amount via floats and `int()` **truncation** (`int(refund_dollars * 100)`), while the response computed it separately with `round()` (banker's rounding). Rule 6 requires round-to-nearest with half-cents **up**, and requires the response amount to equal the RefundLog amount. E.g. 50% of 101 cents: spec says 51; the ledger stored 50, and float error could desync ledger vs response.
- **Fix:** Single exact integer helper `compute_refund_amount_cents(price_cents, percent) = (price_cents * percent + 50) // 100` (round-half-up, no floats), used by both the ledger and the cancel response.

### 18. Concurrent cancels double-refunded
- **File/line:** `app/routers/bookings.py:195-214` (`cancel_booking`), with `_settlement_pause()` widening the window
- **Bug:** The already-cancelled check, refund logging, and status update were unsynchronized, with a 120 ms sleep between logging the refund and committing `status = "cancelled"`. Two concurrent cancels both passed the status check and both wrote a RefundLog — two refunds for one booking (Rule 6 requires exactly one RefundLog and one 200; the loser must get `409 ALREADY_CANCELLED`).
- **Fix:** Wrapped check → refund → status-commit in a `threading.Lock` (`_cancel_lock`) with a `db.refresh(booking)` inside the lock so the second cancel sees the committed `cancelled` status. The sleep was moved after the lock. Verified: 6 concurrent cancels → exactly one 200, five 409s, exactly one RefundLog.

---

## Reporting, Stats & Caching

### 19. Room stats lost updates under concurrent bursts
- **File/line:** `app/services/stats.py:15-26` (`record_create`, `record_cancel`)
- **Bug:** Read-sleep-write on the shared `_stats` dict: concurrent creates/cancels each read the old count/revenue, slept in `_aggregate_pause()`, and wrote back — losing increments, so `/rooms/{id}/stats` disagreed with the actual bookings (Rule 14).
- **Fix:** Guarded read-modify-write with a `threading.Lock`; sleep moved before the lock. Verified stats exact after a concurrent create/cancel burst.

### 20. Stale caches: report not invalidated on booking create; availability not invalidated on cancel; report not invalidated on room create
- **File/lines:** `app/routers/bookings.py:120-122` (create), `app/routers/bookings.py:216-218` (cancel), `app/routers/rooms.py:54-57` (`create_room`)
- **Bug:** Rules 12–13 require reports and availability to reflect current state *immediately*, but:
  - creating a booking invalidated only the availability cache, not the org's usage-report cache → cached reports missed new bookings;
  - cancelling invalidated only the report cache, not the availability cache → cancelled bookings still showed as busy;
  - creating a room didn't invalidate the report cache → cached reports omitted the new (zero-booking) room, which Rule 12 says must be included.
- **Fix:** Booking create now invalidates both availability and report caches; cancel invalidates both; room create invalidates the report cache.

### 21. Cache dict operations were not thread-safe
- **File/line:** `app/cache.py` (all functions)
- **Bug:** `invalidate_report` iterates the cache dict while concurrent requests can insert into it (`set_report`/`set_availability`), which can raise `RuntimeError: dictionary changed size during iteration` → 500 on an otherwise-valid request (Rule 16).
- **Fix:** Added `threading.Lock`s guarding all reads, writes, and invalidations of both caches.

---

## Multi-Tenancy & Export

### 22. Admin export leaked other organizations' bookings
- **File/line:** `app/services/export.py:48-50` (`generate_export`)
- **Bug:** With `include_all=true&room_id=<id>`, `fetch_bookings_raw` queried by room id with **no org filter** — an admin of org A could export every booking of any room in org B. Rule 9: cross-org resource IDs must behave as non-existent (404).
- **Fix:** When `room_id` is provided, the room is first resolved scoped to the caller's org; unknown/cross-org id → `404 ROOM_NOT_FOUND`.

---

## Datetimes

### 23. Input datetimes with a UTC offset were not converted to UTC
- **File/line:** `app/timeutils.py:12-13` (`parse_input_datetime`)
- **Bug:** `dt.replace(tzinfo=None)` **stripped** the offset instead of converting. An input of `2026-07-10T10:00:00+06:00` was stored as 10:00 UTC instead of 04:00 UTC (Rule 1), corrupting conflict checks, quota windows, refund notice, reports, and availability for any offset-carrying input.
- **Fix:** `dt.astimezone(timezone.utc).replace(tzinfo=None)` — convert to UTC, then store naive. Naive inputs remain treated as UTC as-is.

---

## Input Validation

### 26. Malformed booking datetimes crashed with 500 instead of 400
- **File/line:** `app/routers/bookings.py:88-89` (`create_booking`), via `app/timeutils.py:11` (`parse_input_datetime`)
- **Bug:** `POST /bookings` passed `start_time`/`end_time` straight into `datetime.fromisoformat()` with no error handling. Any non-ISO string (e.g. `"banana"`, `""`) raised an unhandled `ValueError` → **500 Internal Server Error**. The service must always answer with a contractual response (Rule 16); an unparseable booking window belongs to `INVALID_BOOKING_WINDOW` (400).
- **Fix:** Wrapped the two parses in `try/except ValueError` raising `AppError(400, "INVALID_BOOKING_WINDOW", ...)`. Verified: garbage/empty `start_time`/`end_time` now return `400 INVALID_BOOKING_WINDOW`, valid inputs unaffected.

---

## Liveness

### 24. Deadlock between concurrent create and cancel notifications
- **File/line:** `app/services/notifications.py:24-35` (`notify_created`, `notify_cancelled`)
- **Bug:** Classic ABBA deadlock: `notify_created` acquired `_email_lock` → `_audit_lock`, while `notify_cancelled` acquired `_audit_lock` → `_email_lock`. A concurrent create + cancel could each hold one lock and wait forever on the other, hanging both requests — and the sleeps inside make the window easy to hit (Rule 16: no combination of concurrent valid requests may hang the service).
- **Fix:** Both functions now acquire the locks in the same order (`_email_lock` before `_audit_lock`), which makes deadlock impossible.

### 25. Long sleeps held inside locks throttled/serialized the whole API
- **File/lines:** `app/routers/bookings.py` (`_pricing_warmup`, `_quota_audit`, `_settlement_pause` call sites), `app/services/ratelimit.py`, `app/services/reference.py`, `app/services/stats.py`
- **Bug:** After introducing the locks required for correctness, the 100–120 ms artificial sleeps would sit inside the critical sections, serializing all booking traffic behind ~0.3 s lock holds per request — degrading concurrent throughput toward a hang under bursts (Rule 16).
- **Fix:** All sleeps were hoisted outside the lock-protected sections (behavior unchanged — they are inert pauses), so each lock is held only for the actual check/write.

---

## Verification

All fixes were verified against a running server:

- Comprehensive black-box suite (`tests/api_live_test.py`, 173 checks) covering every endpoint, every business rule (Rules 1–16), the full error-code contract, and the concurrency guarantees passes end-to-end against the Docker container: `python tests/api_live_test.py`.
- Existing smoke test (`tests/test_smoke.py`) passes.
- 23 sequential functional checks: register/duplicate-username, token lifetime = 900 s, refresh single-use, logout revocation, back-to-back vs overlapping bookings, zero-duration rejection, booking-detail `start_time`, refund tiers (100/50/0) with response == ledger, `ALREADY_CANCELLED`, pagination, member quota (3 then 409, >24 h exempt), admin quota exemption, stats consistency.
- 6 concurrency checks: same-username register race (one 201, seven 409, zero 500), identical-slot booking race (one 201, five 409), 6 concurrent creations → 6 unique reference codes, concurrent cancel (one 200, five 409, exactly one RefundLog), stats exact after the burst.
- Direct unit check of UTC offset conversion (`+06:00` → correct UTC, naive passthrough) and response rendering with explicit UTC designator.

No API contract changes: all paths, status codes, error codes, and JSON field names are exactly as specified.
