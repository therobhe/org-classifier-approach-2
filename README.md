# German Legal-Form Classifier

An async, multi-stage Python pipeline for classifying German organisations by their legal form (Rechtsform). Designed for high throughput, robust coverage, and extensibility.

**Coverage:** ~60-70% offline (regex only), ~85-90% online (with web scraping)
**Speed:** 10,000+ orgs/sec offline, ~150-300 orgs/min online

---

## Features

- **30+ German legal forms**: GmbH, AG, e.V., UG, KG, SE, eG, Stiftung, and more
- **Multi-stage cascade**: Regex → Web scraping → Heuristics → Unknown
- **Offline mode**: Run without internet access (`--offline`)
- **Disk caching**: Avoid redundant lookups across runs
- **Async pipeline**: Concurrent processing with rate limiting
- **Progress tracking**: Real-time progress bars and statistics
- **Configurable CLI**: Typer-based, with options for concurrency, cache, and columns
- **Extensible**: Add new classifiers or data sources easily

---

## Quick Start

```bash
# Install in development mode
pip install -e .

# Test with sample data
python test_pipeline.py

# Process your CSV (offline mode - fast)
org-classifier input.csv output.csv --offline

# Process with web search (slower, better coverage)
org-classifier input.csv output.csv

# Custom options (see --help for all)
org-classifier input.csv output.csv --max-workers 10 --cache-dir .mycache --column Name_Organisation

# Clear cache before processing
org-classifier input.csv output.csv --clear-cache
```

See **[QUICKSTART.md](documentation/QUICKSTART.md)** for detailed usage examples.

---

## Pipeline Stages

1. **Regex name extraction**: Match legal-form suffixes/infixes (60-70% coverage, instant)
2. **Web search + Impressum scrape**: Search and parse company impressum pages (20-25% additional coverage, ~2s/org)
3. **Heuristic fallback**: Conservative keyword matching (5-10% coverage, instant)
4. **Null assignment**: Mark unclassifiable records

---

## CSV Format

**Input:** CSV with organisation names (default column: `organisation_name`)

```csv
organisation_name
Siemens AG
Deutsche Bank AG
FC Bayern München e.V.
```

Semicolon-delimited German CSVs are supported (delimiter + encoding auto-detected). Use `--column` to specify a custom column name.

**Output:** Input + classification columns

```csv
organisation_name,legal_form,confidence,source
Siemens AG,AG,high,name
Deutsche Bank AG,AG,high,name
FC Bayern München e.V.,e.V.,high,name
```

---

## Documentation

- **[QUICKSTART.md](documentation/QUICKSTART.md)** – Usage examples and common use cases
- **[IMPLEMENTATION.md](documentation/IMPLEMENTATION.md)** – Architecture and design
- **[ARCHITECTURE.md](documentation/ARCHITECTURE.md)** – Pipeline diagrams and flow
- **[PROJECT_SUMMARY.md](documentation/PROJECT_SUMMARY.md)** – Project status and deliverables
- **test_pipeline.py** – Automated test suite

---

## Requirements

- Python 3.9+
- pandas, httpx, beautifulsoup4, lxml
- diskcache, googlesearch-python, tenacity
- pydantic, pydantic-settings, typer, tqdm

---

## License

MIT
