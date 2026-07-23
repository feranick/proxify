#!/usr/bin/env python3
"""
CSV-driven variant of proxify.py.

Takes a metadata CSV (one paper per row) instead of a plain URL/DOI list, and
uses the columns to work smarter:

  * `pdf_url` / `landing_url` are used directly, so most rows need **no** DOI
    resolution at all (much faster than the .txt workflow).
  * open-access rows that have a direct `pdf_url` are fetched **directly,
    without the proxy** (they're free, and often on hosts the proxy doesn't
    cover).
  * closed / `needs_library` rows (or rows with only a DOI) are routed through
    the library proxy, resolving the DOI first when `-r` is given.
  * saved files are named from the paper **title + year** (no spaces).

Set your institution's proxy host via the LIBPROXY_HOST environment variable or
the --proxy-host flag.

Expected CSV columns (header row; extras are ignored, missing ones tolerated):
    doi, title, authors, year, journal, publisher, ...,
    unpaywall_is_oa, oa_status, pdf_url, landing_url, ..., doi_url, access_class

All the heavy lifting (proxying, PDF-guessing, curl download, abstract
extraction, JS-gated routing) is shared with proxify.py — this file only
adds the CSV front-end. For a plain .txt list of URLs/DOIs, use proxify.py.

Usage:
    python3 proxify_csv.py access_all_papers.csv -d -c cookies.txt
    python3 proxify_csv.py access_all_papers.csv -r -g -j 40 -d -c cookies.txt
"""

import argparse
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

import proxify as pm


def _resolve(items, jobs, unresolved_file):
    """Resolve doi-like sources in `items` to publisher URLs. Returns a map
    {src -> resolved} and writes an unresolved-DOI file (unresolved_file) if any fail."""
    doi_srcs = sorted({it["src"] for it in items if pm.is_doi_like(it["src"])})
    resolved = {}
    if not doi_srcs:
        return resolved
    total, workers = len(doi_srcs), max(1, jobs)
    print(f"Resolving {total} DOI(s) with {workers} worker(s) "
          f"(rows with a pdf_url/landing_url are skipped)...")
    results, done = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(pm.resolve_doi, u): u for u in doi_srcs}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                results[s] = fut.result()
            except Exception:
                results[s] = pm.normalize_doi(s)
            done += 1
            if done % 25 == 0 or done == total:
                print(f"\r  resolved {done}/{total}", end="", flush=True)
    print()
    unresolved = []
    for s in doi_srcs:
        resolved[s] = results[s]
        if (urlsplit(results[s]).hostname or "").lower() in pm.DOI_HOSTS:
            unresolved.append(s)
    print(f"Resolved {total - len(unresolved)}/{total} DOI(s) to a publisher URL "
          f"({len(unresolved)} could not be resolved).")
    if unresolved:
        with open(unresolved_file, "w", encoding="utf-8") as f:
            f.write("# DOIs that could not be resolved to a publisher URL\n")
            for s in unresolved:
                f.write(s + "\n")
        print(f"Wrote {len(unresolved)} unresolved DOI(s) to {unresolved_file}")
    return resolved


def build_records(items, args, resolved):
    """Turn parsed CSV items into download records for pm.download()."""
    def transform(it):
        src = it["src"]
        if args.resolve_doi and pm.is_doi_like(src):
            src = resolved.get(src, src)
        # arXiv: open access, fetch directly (no proxy)
        if not args.no_arxiv_oa and pm.is_arxiv(src):
            return pm.arxiv_pdf_url(src), False
        # a URL that's already in proxied form (e.g. re-running a report CSV)
        if it["already_proxied"]:
            return src, pm.is_js_gated(src)
        # only guess when the CSV did NOT already give us a direct PDF link
        if args.pdf_guess and not it["has_direct_pdf"]:
            src = pm.guess_pdf_url(src)
        # open-access + a real PDF link -> fetch directly, skip the proxy
        if it["is_oa"] and it["has_direct_pdf"]:
            return src, False
        # everything else goes through the library proxy
        return pm.proxify(src, mode=args.mode), pm.is_js_gated(src)

    records = []
    for it in items:
        url, gated = transform(it)
        # <FirstAuthorSurname>_<Title>_<Year>; reuse a precomputed name if the
        # input was one of our report CSVs.
        name = it.get("name") or pm.filename_from_meta(
            it.get("authors", ""), it["title"], it["year"]) or None
        records.append({
            "orig": it["id"], "proxied": url, "gated": gated,
            "title": it["title"], "year": it["year"], "name": name,
        })
    return records


def main():
    ap = argparse.ArgumentParser(
        description="Download papers listed in a metadata CSV via a library proxy.")
    ap.add_argument("infile", help="metadata CSV (doi/title/year/pdf_url/landing_url/...)")
    ap.add_argument("outfile", nargs="?", default=None,
                    help="URL list output (default: <outroot>/proxied.txt)")
    ap.add_argument("--proxy-host", default=None,
                    help="your EZproxy host (overrides LIBPROXY_HOST), e.g. libproxy.example.edu")
    ap.add_argument("--outroot", default=None,
                    help="parent folder for all output (default: the CSV name without .csv)")
    ap.add_argument("-m", "--mode", choices=["host", "login"], default="host",
                    help="proxy style (default: host)")
    ap.add_argument("-d", "--download", action="store_true", help="download with curl")
    ap.add_argument("-o", "--outdir", default=None, help="PDF dir (default: <outroot>/downloads)")
    ap.add_argument("--pagedir", "--abstractdir", default=None, dest="pagedir",
                    help="saved-HTML-page dir when no PDF (default: <outroot>/abstract_failed)")
    ap.add_argument("-c", "--cookies", default=None,
                    help="Netscape cookies.txt for proxy authentication")
    ap.add_argument("-g", "--pdf-guess", action="store_true",
                    help="rewrite landing/viewer URLs to direct-PDF URLs")
    ap.add_argument("-f", "--failfile", default=None,
                    help="failed-links CSV (default: <outroot>/failed.csv)")
    ap.add_argument("-b", "--browserfile", default=None,
                    help="needs-browser CSV (default: <outroot>/needs_browser.csv)")
    ap.add_argument("-r", "--resolve-doi", action="store_true",
                    help="resolve DOIs for rows that have only a DOI (no pdf/landing URL)")
    ap.add_argument("-j", "--jobs", type=int, default=8,
                    help="parallel workers for DOI resolution (default: 8)")
    ap.add_argument("-u", "--unresolved-file", default=None,
                    help="unresolved-DOI file (default: <outroot>/unresolved.txt)")
    ap.add_argument("--no-arxiv-oa", action="store_true",
                    help="do NOT bypass the proxy for arXiv (default: bypass)")
    args = ap.parse_args()
    pm.set_proxy_host(args.proxy_host)
    if pm.PROXY_SUFFIX == "libproxy.example.edu":
        print("Warning: proxy host not set — using placeholder 'libproxy.example.edu'. "
              "Set LIBPROXY_HOST or pass --proxy-host for working links.")

    items = pm.load_csv_items(args.infile)
    if not items:
        print("No usable rows found (need a doi / pdf_url / landing_url / proxied_url column).")
        return
    n_direct = sum(1 for it in items if it["is_oa"] and it["has_direct_pdf"])
    n_doi_only = sum(1 for it in items if pm.is_doi_like(it["src"]))
    print(f"Loaded {len(items)} row(s) from {args.infile}: "
          f"{n_direct} open-access PDF(s) fetched directly, "
          f"{n_doi_only} row(s) are DOI-only"
          + (" (use -r to resolve them)." if n_doi_only and not args.resolve_doi else "."))

    # Everything for this CSV lives inside one folder named after it.
    root = args.outroot or pm.output_root(args.infile)
    os.makedirs(root, exist_ok=True)
    outfile = args.outfile or os.path.join(root, "proxied.txt")
    outdir = args.outdir or os.path.join(root, "downloads")
    pagedir = args.pagedir or os.path.join(root, "abstract_failed")
    unres_file = args.unresolved_file or os.path.join(root, "unresolved.txt")

    resolved = _resolve(items, args.jobs, unres_file) if args.resolve_doi else {}

    records = build_records(items, args, resolved)

    with open(outfile, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(rec["proxied"] + "\n")
    print(f"Wrote {len(records)} URL(s) to {outfile}")

    if not args.download:
        return
    if args.mode == "login" and not args.cookies:
        print("Note: login mode without -c/--cookies will just fetch the login page.")

    failures, needs_browser = pm.download(
        records, outdir, args.cookies, pagedir)

    if failures:
        ff = args.failfile or os.path.join(root, "failed.csv")
        pm.write_report_csv(ff, failures)
        print(f"Wrote {len(failures)} failed link(s) to {ff}")
    if needs_browser:
        bf = args.browserfile or os.path.join(root, "needs_browser.csv")
        pm.write_report_csv(bf, needs_browser)
        print(f"Wrote {len(needs_browser)} browser-required link(s) to {bf}")
    if not failures and not needs_browser:
        print("All links produced a valid PDF.")


if __name__ == "__main__":
    main()
