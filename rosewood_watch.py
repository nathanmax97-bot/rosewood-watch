"""Rosewood promo-rate watcher (no browser needed).

Reads the same rate-calendar data the booking page uses, with your promo code
applied, for every bookable hotel in your chosen regions. Emails you whenever a
date shows a rate at or below max_price that was not there on the previous sweep
(so a date that disappears and comes back, e.g. a cancellation, alerts again).
Each alert links straight to that hotel and date with your code already applied.
If the site rejects your promo code on every request, you get one warning email.

Setup:   pip3 install requests
Email:   set SMTP_USER and SMTP_PASS (Gmail app password) as environment variables
Run:     python3 rosewood_watch.py --once     one sweep, prints what it found
         python3 rosewood_watch.py            sweep forever
"""
import json, os, smtplib, sys, time
from datetime import date, timedelta
from email.message import EmailMessage
from urllib.parse import quote
import requests

CFG = json.load(open("config.json"))
for _env, _key in (("PROMO_CODE", "promo_code"), ("EMAIL_TO", "email_to")):
    if os.environ.get(_env):
        CFG[_key] = os.environ[_env]  # lets GitHub secrets override the config file
BASE = "https://www.rosewoodhotels.com"
HOTEL_LIST = BASE + "/content/dam/rosewoodhotels/json/brand/hotels/en/rosewood-hotel.json"
SEEN_FILE = "seen.json"
CODE_FLAG = "__code_rejected__"
HEADERS = {"User-Agent": "rosewood-watch/1.0 (personal use)", "Accept": "application/json"}


def load_seen():
    try:
        return set(json.load(open(SEEN_FILE)))
    except FileNotFoundError:
        return set()


def save_seen(seen):
    json.dump(sorted(seen), open(SEEN_FILE, "w"))


def send_email(subject, body):
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = CFG["email_to"]
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def get_hotels(session):
    data = session.get(HOTEL_LIST, timeout=30).json()["hotels"]
    return [
        h for h in data
        if h.get("bookable") is True
        and h.get("code") not in (None, "", "N/A")
        and h["continent"]["id"] in CFG["regions"]
    ]


def month_windows(months):
    """Calendar-month windows starting today, like the booking page requests."""
    start = date.today()
    for i in range(months):
        first = start if i == 0 else (start.replace(day=1) + timedelta(days=32 * i)).replace(day=1)
        nxt = (first.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield first, nxt - timedelta(days=1)


def fetch_month(session, hotel, start, end, promo):
    url = (
        f"{BASE}/api/catalog/v1/room/calendar?hotelId={quote(hotel['code'])}"
        f"&startDate={start}&endDate={end}&numRooms=1&adults={CFG['adults']}"
        f"&childrenCountPerRoom=0&adultsPerRoom={CFG['adults']}"
        + (f"&promoCode={quote(promo)}" if promo else "")
        + f"&currencyCode=EUR&timeZone={quote(hotel['timeZone'], safe='')}"
        f"&userSelectedDates=false&calStartDate={start}&calEndDate={end}"
    )
    r = session.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


def find_hits(payload, max_price):
    """Return [(date, amount, currency_symbol, min_stay, room_code)] at or below max_price."""
    hits = []
    for hotel in payload.get("hotels", []):
        for day in hotel.get("availability", []):
            rate = day.get("rate") or {}
            amount = rate.get("amount")
            if day.get("available") and amount is not None and amount <= max_price:
                hits.append((day["date"], amount, rate.get("currencySymbol") or rate.get("currency", ""),
                             day.get("minStay", "1"), day.get("roomCode", "")))
    return hits


def code_rejected(payload):
    return any(w.get("Code") == "InvalidPromoOrCorporateCode" for w in payload.get("warning", []))


def booking_link(hotel_code, check_in, min_stay):
    """Link to the hotel's room page for that date with the promo code already applied."""
    try:
        nights = max(1, int(min_stay))
    except (TypeError, ValueError):
        nights = 1
    check_out = date.fromisoformat(check_in) + timedelta(days=nights)
    return (
        f"{BASE}/en/{hotel_code}/reservations/room-availability?hotelId={quote(hotel_code)}"
        f"&promoCode={quote(CFG['promo_code'])}&checkInDate={check_in}&checkOutDate={check_out}"
        f"&numRooms=1&adults={CFG['adults']}&childrenCountPerRoom=0&adultsPerRoom={CFG['adults']}"
    )


def sweep(session, previous):
    """Returns (current_keys, new_lines, code_problem). A key is 'hotel|date'.

    If a request fails, that hotel/month keeps its previous keys, so a network
    blip does not cause a false 'new' alert on the next sweep.
    code_problem is True when the site rejected the promo code on every request.
    """
    current, lines = set(), []
    ok = rejected = 0
    for hotel in get_hotels(session):
        for start, end in month_windows(CFG["months_ahead"]):
            month = str(start)[:7]
            try:
                payload = fetch_month(session, hotel, start, end, CFG["promo_code"])
            except Exception as e:
                print(f"skip {hotel['name'].strip()} {month}: {str(e)[:60]}")
                current |= {k for k in previous if k.startswith(f"{hotel['code']}|{month}")}
                continue
            ok += 1
            if code_rejected(payload):
                rejected += 1
            for d, amount, cur, min_stay, room in find_hits(payload, CFG["max_price"]):
                key = f"{hotel['code']}|{d}"
                current.add(key)
                if key not in previous:
                    stay = f", min stay {min_stay} nights" if str(min_stay) not in ("1", "") else ""
                    lines.append(f"{hotel['name'].strip()}  {d}  {cur}{amount:.2f}  (room {room}{stay})\n"
                                 f"{booking_link(hotel['code'], d, min_stay)}")
            time.sleep(CFG["delay_between_requests_s"])
    code_problem = ok > 0 and rejected == ok
    if code_problem:
        print("NOTE: the site rejected the promo code on every request.")
    return current, lines, code_problem


def main():
    once = "--once" in sys.argv
    session = requests.Session()
    session.headers.update(HEADERS)
    previous = load_seen()
    while True:
        print(time.strftime("%H:%M:%S"), "sweep start")
        current, lines, code_problem = sweep(session, previous)
        print(f"{len(lines)} new hit(s), {len(current)} available in total")
        # Email first: if sending fails, results are not saved and the next run tries again.
        if code_problem and CODE_FLAG not in previous:
            send_email("Rosewood: your promo code is being rejected",
                       "The site rejected your promo code on every request. It has probably expired "
                       "or changed. Update the PROMO_CODE secret in GitHub (Settings > Secrets and "
                       "variables > Actions). You will not get alerts until it is fixed.")
            print("emailed code warning")
        if code_problem:
            current.add(CODE_FLAG)
        if lines:
            send_email(f"Rosewood: {len(lines)} new promo rate(s)", "\n\n".join(lines))
            print("emailed")
        save_seen(current)
        previous = current
        if once:
            break
        time.sleep(CFG["sweep_interval_min"] * 60)


if __name__ == "__main__":
    main()
