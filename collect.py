#!/usr/bin/env python3
"""
collect.py — daily raw collector for justjoin.it job offers.

Downloads the complete offer list from the justjoin.it user-panel API and stores
the responses verbatim (gzipped, one file per day) under
    data/raw/justjoin/YYYY-MM-DD.json.gz

The raw layer is inviolable: response bodies are stored as received (exact UTF-8
text, not re-serialized), together with run metadata and control numbers.

Completeness is proven, not assumed — see check_* functions and NOTES.md.
Any problem => non-zero exit code + message on stderr. Nothing is written unless
every check passes.

Exit codes:
    0  success
    1  validation failed (incomplete / inconsistent data) — nothing written
    2  network failure (retries exhausted) — nothing written
    3  local I/O failure

Stdlib only. Works on Python 3.9 (macOS) and 3.12 (GitHub Actions).
"""

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------
# Constants established empirically during recon (see NOTES.md)
# --------------------------------------------------------------------------

API_BASE = "https://api.justjoin.it"
LIST_PATH = "/v2/user-panel/offers/by-cursor"
COUNT_PATH = "/v2/user-panel/offers/count"

# Hard server-side limit: itemsCount > 100 => HTTP 400
# ("itemsCount must not be greater than 100").
PAGE_SIZE = 100

# sortBy=newest + orderBy=ASC is the only ordering proven to be
#   * total and stable (lastPublishedAt strictly non-decreasing), and
#   * lossless (the default sortBy=published drops rows — see NOTES.md).
# ASC is chosen over DESC so that offers published *during* the run are appended
# at the tail instead of shifting the whole window and causing skips.
SORT_BY = "newest"
ORDER_BY = "ASC"

# Contact/homepage can be overridden without touching the code; the point of the
# UA is that a justjoin.it admin can tell who we are and reach us.
CONTACT = os.environ.get("JJIT_CONTACT", "adam.choynowski1@gmail.com")
USER_AGENT = "jjit-collector/1.0 (daily job-market research; contact: %s)" % CONTACT

# Politeness / resilience
REQUEST_DELAY_S = 0.5
TIMEOUT_S = 30
MAX_ATTEMPTS_PER_REQUEST = 5
BACKOFF_BASE_S = 2.0  # 2, 4, 8, 16 s
MAX_CRAWL_ATTEMPTS = 3  # a whole-crawl retry, used when the board changed mid-run

OUT_DIR = os.path.join("data", "raw", "justjoin")


class Fatal(Exception):
    """Unrecoverable problem; carries the process exit code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def log(msg):
    sys.stderr.write("[%s] %s\n" % (dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def http_get(url):
    """GET `url`, return (body_text, status). Retries with growing backoff.

    Headers sent — the minimal set that works (justified in NOTES.md):
      User-Agent      required: Cloudflare answers 403 to the default
                      "Python-urllib/3.x". Also identifies the project.
      Accept          declares the expected representation; costs nothing.
      Accept-Encoding saves ~6x bandwidth for the service (26 kB vs 166 kB/page).
    No cookies, no auth, no session headers of any kind.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    last_err = None
    for attempt in range(1, MAX_ATTEMPTS_PER_REQUEST + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8"), resp.status
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            # 4xx other than 429 is a bug in our request, not a transient fault.
            if e.code != 429 and 400 <= e.code < 500:
                raise Fatal(2, "HTTP %d for %s: %s" % (e.code, url, body))
            last_err = "HTTP %d: %s" % (e.code, body)
        except Exception as e:  # URLError, socket timeout, gzip/decode errors
            last_err = "%s: %s" % (type(e).__name__, e)

        if attempt < MAX_ATTEMPTS_PER_REQUEST:
            wait = BACKOFF_BASE_S ** attempt
            log("request failed (%s) — attempt %d/%d, retrying in %.0fs: %s"
                % (last_err, attempt, MAX_ATTEMPTS_PER_REQUEST, wait, url))
            time.sleep(wait)

    raise Fatal(2, "giving up on %s after %d attempts: %s"
                % (url, MAX_ATTEMPTS_PER_REQUEST, last_err))


def build_list_url(cursor, sort_by=SORT_BY, order_by=ORDER_BY, page_size=PAGE_SIZE):
    q = urllib.parse.urlencode({
        "itemsCount": page_size,
        "from": cursor,
        "sortBy": sort_by,
        "orderBy": order_by,
    })
    return "%s%s?%s" % (API_BASE, LIST_PATH, q)


def fetch_location_row_count():
    """Independent control number.

    /v2/user-panel/offers/count does NOT return the number of offers — it returns
    the number of offer*location rows (an offer listed in 5 cities counts 5x).
    Proven in recon: sum(len(offer.multilocation)) over the full crawl == this
    number, exactly (19713 == 19713 on 2026-08-09).
    It is computed by a different endpoint over a different aggregation, so it is
    a genuinely independent witness of completeness.
    """
    body, _ = http_get(API_BASE + COUNT_PATH)
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("count"), int):
        raise Fatal(1, "unexpected payload from %s: %s" % (COUNT_PATH, body[:200]))
    return data["count"]


# --------------------------------------------------------------------------
# Crawl
# --------------------------------------------------------------------------

def crawl(sort_by, order_by, page_size):
    """Walk the cursor to the end. Returns (pages, offers).

    `pages` holds the verbatim response bodies (raw layer).
    `offers` is a parsed view used *only* for validation.
    """
    pages = []
    offers = []
    cursor = 0
    seen_cursors = set()

    while True:
        if cursor in seen_cursors:
            raise Fatal(1, "cursor loop detected at from=%s" % cursor)
        seen_cursors.add(cursor)

        url = build_list_url(cursor, sort_by, order_by, page_size)
        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
        body, status = http_get(url)
        try:
            parsed = json.loads(body)
        except ValueError as e:
            raise Fatal(1, "non-JSON response from %s: %s" % (url, e))
        if not isinstance(parsed, dict) or "data" not in parsed or "meta" not in parsed:
            raise Fatal(1, "unexpected list payload from %s: %s" % (url, body[:200]))

        pages.append({
            "url": url,
            "http_status": status,
            "fetched_at_utc": fetched_at,
            "body": body,  # verbatim, unparsed
        })
        batch = parsed["data"]
        offers.extend(batch)
        meta = parsed["meta"]
        log("from=%-6s got=%-4d total=%-6s next=%s"
            % (cursor, len(batch), meta.get("totalItems"), (meta.get("next") or {}).get("cursor")))

        nxt = (meta.get("next") or {}).get("cursor")
        if not batch or nxt is None:
            break
        cursor = nxt
        time.sleep(REQUEST_DELAY_S)

    return pages, offers


# --------------------------------------------------------------------------
# Validation — every check must pass or the day is not written
# --------------------------------------------------------------------------

def validate(pages, offers, count_before, count_after, page_size):
    """Return (checks, expected_offers, location_rows). Raises Fatal on failure."""
    problems = []
    checks = []

    def check(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            problems.append("%s — %s" % (name, detail))

    metas = [json.loads(p["body"])["meta"] for p in pages]
    totals = sorted(set(m.get("totalItems") for m in metas))

    # C1: the API must have reported one consistent total for the whole run.
    check("totalItems_stable_across_pages", len(totals) == 1,
          "reported totals: %s" % totals)
    expected = totals[0] if len(totals) == 1 else None

    # C2: what we actually collected equals what the API said exists.
    n = len(offers)
    check("collected_equals_totalItems", expected is not None and n == expected,
          "collected=%d expected=%s" % (n, expected))

    # C3: no duplicates — a duplicate means the window shifted and something was skipped.
    guids = [o.get("guid") for o in offers]
    uniq = len(set(guids))
    check("no_duplicate_guids", uniq == n, "unique=%d collected=%d" % (uniq, n))
    check("no_missing_guids", all(guids), "records without guid: %d" % sum(1 for g in guids if not g))

    # C4: pagination really reached the end, and covered every window.
    if expected is not None:
        want_pages = max(1, -(-expected // page_size))
        check("page_count_matches_total", len(pages) == want_pages,
              "pages=%d expected=%d" % (len(pages), want_pages))
    last_next = (metas[-1].get("next") or {}).get("cursor")
    check("last_page_terminates", last_next is None, "last next.cursor=%s" % last_next)

    # C5: the ordering key is monotonic over the *whole* concatenated result.
    # This is what makes offset-cursor paging safe: a total order means the
    # windows tile the list without gaps or overlaps.
    ts = [o.get("lastPublishedAt") or "" for o in offers]
    bad = next((i for i in range(len(ts) - 1) if ts[i] > ts[i + 1]), None)
    check("sort_key_monotonic_asc", bad is None,
          "ok" if bad is None else "order breaks at index %d (%s > %s)" % (bad, ts[bad], ts[bad + 1]))

    # C6: independent witness — offer*location rows counted by a different endpoint.
    location_rows = sum(len(o.get("multilocation") or []) for o in offers)
    check("location_rows_match_count_endpoint",
          count_before == count_after == location_rows,
          "sum(multilocation)=%d count_before=%d count_after=%d"
          % (location_rows, count_before, count_after))

    if problems:
        raise Fatal(1, "completeness validation FAILED:\n  - " + "\n  - ".join(problems))
    return checks, expected, location_rows


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_archive(path, document):
    tmp = path + ".tmp-%d" % os.getpid()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = json.dumps(document, ensure_ascii=False, sort_keys=False).encode("utf-8")
        # mtime=0 => byte-identical output for identical input (reproducible archives)
        with open(tmp, "wb") as fh:
            with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0) as gz:
                gz.write(payload)
        os.replace(tmp, path)  # atomic: a partial file can never look like a good day
        return len(payload)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise Fatal(3, "cannot write %s: %s" % (path, e))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run_once(sort_by, order_by, page_size):
    started = dt.datetime.now(dt.timezone.utc)
    count_before = fetch_location_row_count()
    log("control: /offers/count (location rows) before crawl = %d" % count_before)
    pages, offers = crawl(sort_by, order_by, page_size)
    time.sleep(REQUEST_DELAY_S)
    count_after = fetch_location_row_count()
    log("control: /offers/count (location rows) after crawl  = %d" % count_after)
    checks, expected, location_rows = validate(pages, offers, count_before, count_after, page_size)
    finished = dt.datetime.now(dt.timezone.utc)
    return {
        "pages": pages,
        "offers": offers,
        "checks": checks,
        "expected": expected,
        "location_rows": location_rows,
        "count_before": count_before,
        "count_after": count_after,
        "started": started,
        "finished": finished,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--date", default=None,
                    help="archive date, YYYY-MM-DD (default: today, UTC)")
    # The three knobs below exist so the validation itself can be tested by
    # deliberately breaking the request (acceptance criterion 4).
    ap.add_argument("--sort-by", default=SORT_BY, help=argparse.SUPPRESS)
    ap.add_argument("--order-by", default=ORDER_BY, help=argparse.SUPPRESS)
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE, help=argparse.SUPPRESS)
    ap.add_argument("--sort-param-name", default="sortBy", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    # Allows simulating "somebody renamed a parameter" without touching the code.
    if args.sort_param_name != "sortBy":
        global build_list_url
        original = build_list_url

        def build_list_url(cursor, sort_by=SORT_BY, order_by=ORDER_BY, page_size=PAGE_SIZE):
            url = original(cursor, sort_by, order_by, page_size)
            return url.replace("sortBy=", args.sort_param_name + "=")

    day = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    out_path = os.path.join(args.out_dir, "%s.json.gz" % day)

    last_error = None
    for attempt in range(1, MAX_CRAWL_ATTEMPTS + 1):
        log("crawl attempt %d/%d" % (attempt, MAX_CRAWL_ATTEMPTS))
        try:
            r = run_once(args.sort_by, args.order_by, args.page_size)
            break
        except Fatal as e:
            last_error = e
            if e.code == 1 and attempt < MAX_CRAWL_ATTEMPTS:
                # The most likely cause of a consistency failure is the board
                # changing mid-crawl. A fresh snapshot usually fixes it; a real
                # breakage will fail identically on every attempt.
                log("%s" % e)
                log("retrying with a fresh snapshot in 30s")
                time.sleep(30)
                continue
            sys.stderr.write("FATAL: %s\n" % e)
            return e.code
    else:
        sys.stderr.write("FATAL: %s\n" % last_error)
        return last_error.code

    document = {
        "schema": "justjoin-raw/1",
        "source": "justjoin.it",
        "collected_at_utc": r["started"].isoformat(),
        "finished_at_utc": r["finished"].isoformat(),
        "duration_s": round((r["finished"] - r["started"]).total_seconds(), 1),
        "collector": {
            "script": "collect.py",
            "user_agent": USER_AGENT,
            "python": sys.version.split()[0],
            "request_delay_s": REQUEST_DELAY_S,
        },
        "request": {
            "endpoint": API_BASE + LIST_PATH,
            "count_endpoint": API_BASE + COUNT_PATH,
            "sort_by": args.sort_by,
            "order_by": args.order_by,
            "page_size": args.page_size,
            "urls": [p["url"] for p in r["pages"]],
        },
        "control_numbers": {
            "offers_collected": len(r["offers"]),
            "offers_expected_totalItems": r["expected"],
            "unique_guids": len(set(o.get("guid") for o in r["offers"])),
            "pages": len(r["pages"]),
            "location_rows_from_offers": r["location_rows"],
            "location_rows_count_endpoint_before": r["count_before"],
            "location_rows_count_endpoint_after": r["count_after"],
        },
        "checks": r["checks"],
        # ---- raw layer: response bodies exactly as received ----
        "pages": r["pages"],
    }
    document["control_numbers"]["payload_sha256"] = hashlib.sha256(
        "".join(p["body"] for p in r["pages"]).encode("utf-8")).hexdigest()

    size = write_archive(out_path, document)
    gz_size = os.path.getsize(out_path)

    n = len(r["offers"])
    print("OK  %s" % out_path)
    print("    collected : %d offers (%d pages, %.1f MB raw -> %.1f MB gz)"
          % (n, len(r["pages"]), size / 1e6, gz_size / 1e6))
    print("    expected  : %d offers" % r["expected"])
    print("    why we know the expected number:")
    print("      * every one of the %d responses reported meta.totalItems=%d"
          % (len(r["pages"]), r["expected"]))
    print("      * pagination terminated by itself (next.cursor=null) after exactly")
    print("        ceil(%d/%d)=%d pages, with %d distinct guids and no duplicates"
          % (r["expected"], args.page_size, len(r["pages"]), n))
    print("      * ordering key lastPublishedAt is monotonic over all %d records," % n)
    print("        so the cursor windows tile the list with no gaps or overlaps")
    print("      * independent witness: /offers/count returns offer*location rows,")
    print("        %d before and %d after the crawl; sum(multilocation) over the"
          % (r["count_before"], r["count_after"]))
    print("        collected offers is %d — all three agree" % r["location_rows"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fatal as e:
        sys.stderr.write("FATAL: %s\n" % e)
        sys.exit(e.code)
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        sys.exit(130)
