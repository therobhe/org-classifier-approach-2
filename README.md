# German Legal-Form Classifier

Multi-stage, async Python pipeline that classifies German organisations by their legal form (Rechtsform).

**Coverage:** ~60-70% offline (regex only), ~85-90% online (with web scraping)  
**Speed:** 10,000+ orgs/sec offline, ~150-300 orgs/min online

## Quick Start

```bash
# Install
pip install -e .

# Test with sample data
python test_pipeline.py

# Process your CSV (offline mode - fast!)
org-classifier input.csv output.csv --offline

# Process with web search (slower, better coverage)
org-classifier input.csv output.csv
```

See **[QUICKSTART.md](QUICKSTART.md)** for detailed usage examples.

## Features

- ✅ **30+ German legal forms** - GmbH, AG, e.V., UG, KG, SE, eG, Stiftung, etc.
- ✅ **Multi-stage cascade** - Regex → Web scraping → Heuristics → Unknown
- ✅ **Offline mode** - Run without internet access (`--offline`)
- ✅ **Disk caching** - Avoid redundant lookups across runs
- ✅ **Async pipeline** - Concurrent processing with rate limiting
- ✅ **Progress tracking** - Real-time progress bars and statistics

## Pipeline Stages

1. **Regex name extraction** - Match legal-form suffixes/infixes (60-70% coverage, instant)
2. **Web search + Impressum scrape** - Search and parse company impressum pages (20-25% additional coverage, ~2s/org)
3. **Heuristic fallback** - Conservative keyword matching (5-10% coverage, instant)
4. **Null assignment** - Mark unclassifiable records

## CSV Format

**Input:** CSV with organisation names

```csv
organisation_name
Siemens AG
Deutsche Bank AG
FC Bayern München e.V.
```

Semicolon-delimited German CSVs are supported (delimiter + encoding are auto-detected). If your name column is called `Name_Organisation`, the pipeline will auto-detect it, or you can specify it via `--column Name_Organisation`.

**Output:** Input + classification columns

```csv
organisation_name,legal_form,confidence,source
Siemens AG,AG,high,name
Deutsche Bank AG,AG,high,name
FC Bayern München e.V.,e.V.,high,name
```

## Documentation

- **[QUICKSTART.md](QUICKSTART.md)** - Usage examples and common use cases
- **[IMPLEMENTATION.md](IMPLEMENTATION.md)** - Architecture details and design decisions
- **[sample_input.csv](sample_input.csv)** - Sample test data
- **[test_pipeline.py](test_pipeline.py)** - Automated test suite

## Requirements

Python 3.9+ with dependencies:

- pandas, httpx, beautifulsoup4, lxml
- diskcache, googlesearch-python, tenacity
- pydantic, pydantic-settings, typer, tqdm

## License

MIT
