# paper-collect

Reusable paper collection utilities for large-scale literature corpora.

The first supported workflow builds a seed manifest from a local DBLP XML dump.
Downstream projects can then enrich those seed rows with venue pages, abstracts,
PDF links, downloaded PDFs, and extracted full text.

## Current Scope

* Stream-parse `dblp.xml.gz` without expanding the full file on disk.
* Select main-conference papers by exact DBLP `crossref`.
* Support the first security corpus venues:
	* `sp`: IEEE Symposium on Security and Privacy
	* `ccs`: ACM CCS
	* `security`: USENIX Security Symposium
	* `ndss`: Network and Distributed System Security Symposium
* Store a normalized seed manifest in SQLite.
* Keep raw downloads and generated databases out of git.

## Data Layout

```text
data/
  raw/
    dblp/
      dblp.xml.gz
      dblp.dtd
  paper_collect.sqlite
```

DBLP's XML export is metadata only. It includes titles, authors, years,
booktitles, pages, crossrefs, DBLP page URLs, and electronic-edition links.
It does not include abstracts or paper full text.

## Commands

Run commands from the repository root:

```bash
PYTHONPATH=src python3 -m paper_collect.cli dblp-summary \
  --xml data/raw/dblp/dblp.xml.gz \
  --max-year 2022
```

```bash
PYTHONPATH=src python3 -m paper_collect.cli dblp-sample \
  --xml data/raw/dblp/dblp.xml.gz \
  --max-year 2022 \
  --limit 5
```

```bash
PYTHONPATH=src python3 -m paper_collect.cli dblp-import \
  --xml data/raw/dblp/dblp.xml.gz \
  --db data/paper_collect.sqlite \
  --max-year 2022
```

After importing, inspect the manifest with:

```bash
sqlite3 data/paper_collect.sqlite \
  'select venue, count(*) from papers group by venue order by venue;'
```

## Next Adapters

* Venue pages:
	* USENIX Security proceedings pages usually expose paper pages, abstracts,
	  and PDF links directly.
	* IEEE S&P, CCS, and NDSS need venue-specific page adapters because DBLP
	  does not carry abstracts.
* Open metadata APIs:
	* OpenAlex, Semantic Scholar, Crossref, and Unpaywall can enrich DOI,
	  abstract, open-access URL, and license fields when official venue pages
	  are incomplete.
* Text extraction:
	* GROBID or S2ORC doc2json should be used for full-text extraction instead
	  of hand-written PDF parsing.
