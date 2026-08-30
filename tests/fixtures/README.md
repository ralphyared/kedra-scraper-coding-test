# Captured fixtures

Real HTML captured from workplacerelations.ie during site reconnaissance (2026-08-28).
All parser unit tests run against these offline — no network in the test suite.

| File | Source | What it proves |
|---|---|---|
| `listing_wrc_2024-01_page1.html` | `/en/search/?decisions=1&from=01/01/2024&to=31/01/2024&body=15376&pageNumber=1` | Listing shape: 10 `li.each-item` per page, `h2.title a`, `span.date`, `span.refNO`, `p.description[title]`, and the `Shows 1 to 10 of N results` total. ~800 KB uncompressed. |
| `case_wrc_inline.html` | WRC case page, 2024 | Document shape 1 — decision inline in `div.content`, no attachment. |
| `case_wrc_inline__refetch.html` | Same URL, fetched seconds later | **The hashing landmine.** Byte-identical except `<!-- Elapsed time: … -->` on line 440, so raw `sha256` differs across fetches. Drives the `content_hash` vs `file_hash` split. |
| `case_labour_court_inline.html` | Labour Court case page, 2024 | Document shape 1 on a second body. |
| `case_eat_pdf_only.html` | EAT import, 2010 | Document shape 2 — empty `div.content` + `div.related-items a.download` → PDF. |
| `case_equality_tribunal_html_plus_pdf.html` | Equality Tribunal, 2000 | Document shape 3 — HTML abstract *and* a source PDF link inside `div.content`. |
| `listing_zero_results.html` | A search with no matches | `div.item-list` is present but **empty**, with no `searchhead` count and no pager. This is what distinguishes "the search legitimately found nothing" from "the fetch failed" -- a failed request has no `item-list` at all. Treating the two alike would let a silently broken partition report zero records as success. |
| `search_form_excerpt.html` | `/en/search/` with no query | **Excerpt, not a full capture.** The form region of the baseline ASP.NET page: `__VIEWSTATE` present (value truncated) to document why the GET querystring route makes viewstate handling unnecessary, plus the body checkboxes that are the authoritative source of the body IDs (EAT=2, Equality Tribunal=1, Labour Court=3, WRC=15376). Trimmed 800 KB → 14 KB; the viewstate value alone was 372 KB. |
| `response_headers.txt` | Case-page response | `Cache-Control: no-cache`, no `ETag`/`Last-Modified` → conditional GET unusable for HTML (it *is* usable for PDFs). |

Regenerate/extend with plain `curl` — every URL above is GET-addressable with no session or viewstate.
