# pl-it-jobmarket

A daily, verifiably complete archive of the Polish IT job market.

Every morning a collector walks the full justjoin.it offer board and stores the
responses verbatim, one gzipped file per day. Around **10,000–10,500 offers per
snapshot**. Nothing is written unless the run can *prove* it saw the whole board.

Job boards are ephemeral by design — an offer appears, is edited, and disappears,
leaving nothing behind. This repository turns that stream into a longitudinal
dataset: which skills are actually in demand, how salaries and seniority mixes
drift, how fast postings turn over, how remote/hybrid/office shares move over
time. None of that is answerable from a single scrape.

---

## Status

| | |
|---|---|
| Source | `justjoin.it` (public user-panel API, no auth) |
| Cadence | daily, 05:00 UTC, via GitHub Actions |
| Snapshot size | ~10,400 offers · ~104 pages · ~2.3 MB gzipped · ~80 s per run |
| Dependencies | **none** — Python standard library only |
| Tested on | Python 3.9 (macOS) and 3.12 (GitHub Actions) |

Raw layer only so far. The analysis layer (see [Roadmap](#roadmap)) is the next
step; the priority was to start accumulating history early, because a day not
collected is a day lost forever.

---

## What a snapshot contains

`data/raw/justjoin/YYYY-MM-DD.json.gz` — a single JSON document:

```
{
  "schema": "justjoin-raw/1",
  "collected_at_utc": ..., "finished_at_utc": ..., "duration_s": ...,
  "collector":       { script, user_agent, python, request_delay_s },
  "request":         { endpoint, sort_by, order_by, page_size, urls[] },
  "control_numbers": { offers_collected, offers_expected_totalItems,
                       unique_guids, pages, location_rows_*, payload_sha256 },
  "checks":          [ { name, ok, detail }, ... ],
  "pages":           [ { url, http_status, fetched_at_utc, body } ]   <-- raw layer
}
```

**The raw layer is inviolable.** `body` holds the response text exactly as
received — not re-serialised, not reshaped, not filtered. Parsing happens
downstream and can be redone at any time against the original bytes; a parsing
mistake made today must never be able to corrupt the archive.

Each offer carries, among others: `guid`, `title`, `companyName`,
`requiredSkills` / `niceToHaveSkills`, `experienceLevel`, `workplaceType`,
`employmentTypes` (with salary ranges), `multilocation`, `publishedAt` /
`lastPublishedAt` / `expiredAt`, `languages`.

Archives are byte-reproducible: gzip is written with `mtime=0`, so identical
input yields an identical file.

---

## How completeness is proven

The hard part of a crawler is not fetching — it is knowing you didn't silently
miss anything. Offset pagination over a live, changing list will skip records
whenever the window shifts underneath you, and it will do so quietly.

Six checks run after every crawl. **Any failure means nothing is written** and
the process exits non-zero:

| # | Check | What it rules out |
|---|---|---|
| C1 | `totalItems` identical on all pages | the board size changed mid-run |
| C2 | offers collected == `totalItems` | short crawl |
| C3 | no duplicate and no missing `guid` | window shifted (a duplicate implies a skip) |
| C4 | page count == `ceil(total/100)` and the last page self-terminates | truncated pagination |
| C5 | `lastPublishedAt` monotonic across *all* records | the ordering is total and stable, so cursor windows tile the list with no gaps or overlaps |
| C6 | `sum(len(multilocation))` == `/offers/count`, before **and** after the crawl | an **independent witness**: a different endpoint, aggregating differently, agrees on the total |

C6 is the strongest of the six. `/v2/user-panel/offers/count` does *not* return
the number of offers — it returns offer×location rows, so an offer listed in
five cities counts five times. Reconstructing that number from the collected
data and matching it exactly (19,713 == 19,713 on 2026-08-09) is a check the
list endpoint cannot fake for us.

C5 is what makes offset paging safe here at all, and it is why the collector
uses `sortBy=newest&orderBy=ASC` rather than the UI default. Ascending order
means offers published *during* a run are appended at the tail instead of
shifting every window; the UI default `sortBy=published` was measured to drop
rows outright.

When a consistency check fails, the collector assumes the board changed mid-run
— the common case — and retries the whole crawl on a fresh snapshot up to three
times. A genuine breakage fails identically every time and surfaces as a failed
Actions run rather than a quietly bad day.

Exit codes: `0` success · `1` validation failed · `2` network failure ·
`3` local I/O failure. The archive is written with `os.replace()`, so a partial
file can never be mistaken for a good day.

---

## Running it

```bash
git clone https://github.com/adam-choynowski/pl-it-jobmarket
cd pl-it-jobmarket
python3 collect.py                      # writes data/raw/justjoin/<today>.json.gz
python3 collect.py --date 2026-08-09    # override the archive date
```

No virtualenv, no `pip install`. On success the script prints the control
numbers together with the reasoning behind them:

```
OK  data/raw/justjoin/2026-08-09.json.gz
    collected : 10375 offers (104 pages, 41.2 MB raw -> 2.3 MB gz)
    expected  : 10375 offers
    why we know the expected number:
      * every one of the 104 responses reported meta.totalItems=10375
      * pagination terminated by itself (next.cursor=null) after exactly
        ceil(10375/100)=104 pages, with 10375 distinct guids and no duplicates
      ...
```

Set `JJIT_CONTACT` to change the contact address in the User-Agent.

---

## Being a good citizen

The collector sends exactly three headers and no cookies, tokens or session
state of any kind. `User-Agent` identifies the project and a contact address, so
a justjoin.it administrator can tell who is calling and reach a human;
`Accept-Encoding: gzip` cuts the bandwidth the service pays for by roughly 6×
(26 kB vs 166 kB per page). Requests are spaced 0.5 s apart — one full pass is
~105 requests in ~80 seconds, once a day. Only public, unauthenticated endpoints
are used. `robots.txt` is respected; nothing here scrapes rendered HTML.

---

## Layout

```
collect.py                        # the collector (stdlib only, ~440 lines)
NOTES.md                          # engineering log from the API recon (Polish)
.github/workflows/collect.yml     # daily schedule + commit of new snapshots
data/raw/justjoin/*.json.gz       # the archive
```

`NOTES.md` is the working log behind every constant in `collect.py`: how the API
was located by grepping 46 Next.js chunks, which orderings were measured to be
lossy, why the `itemsCount` ceiling is 100, which parameter names the client
actually sends, and which earlier conclusions turned out to be wrong. It is kept
because the reasoning is worth more than the result — including the dead ends.

---

## Roadmap

1. **Parse layer** — flatten the raw archive into a normalised schema
   (`offers`, `offer_locations`, `offer_skills`, `offer_salaries`), idempotent
   and re-runnable from raw at any time.
2. **SQL analysis** — skill demand over time, salary distributions by seniority
   and stack, offer lifetime and turnover, remote/hybrid/office drift.
3. **Dashboard** — Power BI over the parsed layer.
4. **Text analysis** — skill extraction from free-text requirements, clustering
   of role titles.
5. **Second source** — NoFluffJobs, for cross-board coverage.

---

## Licence and intended use

Research and portfolio project, not affiliated with justjoin.it. The archive is
kept for aggregate statistical analysis of the job market; it is not a mirror of
the board and is not intended for republishing individual offers.
