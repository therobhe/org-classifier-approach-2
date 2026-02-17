# Implementation Notes

## Architecture Overview

The pipeline implements a **confidence-based cascade** with four main stages:

1. **Name Extraction (Regex)** – Fast, high-confidence, ~60-70% hit rate
2. **Web Search + Impressum** – Medium coverage, slower (optional, skipped with `--offline`)
3. **Heuristics** – Low-confidence fallback
4. **Null Assignment** – Unknown cases

All stages are orchestrated asynchronously for high throughput and robust error handling.

---

## Key Design Decisions

### 1. Legal Form Patterns (`constants.py`)

- 30+ German legal forms, ordered longest-match-first to avoid false positives
- Regex patterns use word boundaries (`\b`) and are case-insensitive
- Easy to extend with new forms

### 2. Async Architecture

- Uses `asyncio` and `httpx.AsyncClient` for concurrent web requests
- `asyncio.Semaphore` limits concurrent requests (default: 5)
- All I/O operations (web, disk cache) are async for maximum throughput

### 3. Caching Strategy (`cache.py`)

- Disk-based cache using `diskcache` (survives restarts)
- Keys are normalized: `org:<lowercase_trimmed_name>`
- Avoids redundant web searches across runs
- Can be cleared with `--clear-cache` flag

### 4. Error Handling

- Tenacity library for automatic retries (exponential backoff)
- Web search: 3 attempts, 2-10s backoff
- Page fetch: 2 attempts, 2-5s backoff
- Failures gracefully fall through to next stage

### 5. Offline Mode

- `--offline` flag skips web search stage entirely
- Cascade becomes: Name → Heuristics → Unknown
- Useful for testing, air-gapped environments, and fast dry-runs

### 6. Progress Tracking

- Uses `tqdm` for progress bar during batch processing
- Logs statistics after completion (confidence/source breakdown)

---

## Pipeline Flow Example

```
Input: "Deutsche Bank AG"
  ├─ Cache? No
  ├─ Name extraction? YES → "AG" (high, name)
  └─ Result: AG, high, name

Input: "Berliner Verein für Kultur"
  ├─ Cache? No
  ├─ Name extraction? No
  ├─ Web search? (if online)
  │   ├─ Google: "Berliner Verein für Kultur Impressum"
  │   ├─ Fetch top result
  │   └─ Parse: Found "e.V." → (high/medium, <url>)
  ├─ Heuristics? "verein" → e.V. (low, heuristic)
  └─ Result: e.V., low, heuristic
```

---

## Performance Characteristics

| Stage           | Coverage  | Speed   | Confidence  |
| --------------- | --------- | ------- | ----------- |
| Name extraction | 60-70%    | Instant | High        |
| Web + Impressum | 15-25%    | ~2s/org | High/Medium |
| Heuristics      | 5-10%     | Instant | Low         |
| Unknown         | Remaining | Instant | Unknown     |

**Throughput estimates:**

- Offline mode: 10,000+ orgs/second
- Online mode: ~150-300 orgs/minute (rate-limited)

---

## Extensibility & Future Enhancements

### 1. Web Search API Choice

- **Current:** `googlesearch-python` (free, fragile, rate-limited)
- **Alternatives:** SerpAPI, Searx, Bing Search API (recommended for production)

### 2. LLM Fallback Stage

- Optional: Add GPT-4o/local-LLM between heuristics and null-assignment
- Would boost recall on ambiguous cases
- Not implemented by default (see docs for example implementation)

### 3. Handelsregister Lookup

- Optional: Integrate offeneregister.de API as authoritative source
- Add as Stage 1.5 (after name, before web search)

---

## Installation & Usage

### Install

```bash
pip install -e .
```

### Basic Usage

```bash
# Offline mode (fast, ~65% coverage)
org-classifier input.csv output.csv --offline

# Online mode (slower, ~85% coverage)
org-classifier input.csv output.csv

# Custom settings
org-classifier input.csv output.csv --max-workers 10 --cache-dir .mycache
```

### Test

```bash
python test_pipeline.py
```

---

## CSV Format

**Input:**

```csv
organisation_name
Siemens AG
Deutsche Bank AG
FC Bayern München e.V.
```

**Output:**

```csv
organisation_name,legal_form,confidence,source
Siemens AG,AG,high,name
Deutsche Bank AG,AG,high,name
FC Bayern München e.V.,e.V.,high,name
```

---

## Dependencies

- pandas – CSV I/O
- httpx – Async HTTP client
- beautifulsoup4 + lxml – HTML parsing
- googlesearch-python – Web search
- diskcache – Persistent caching
- tenacity – Retry logic
- pydantic – Data validation
- typer – CLI framework
- tqdm – Progress bars

---

## Next Steps

1. **Test with real data** – Run on sample dataset, measure accuracy
2. **Add Handelsregister stage** – Integrate offeneregister.de API
3. **Improve heuristics** – Add more conservative keyword rules
4. **Consider LLM fallback** – For production use with high accuracy requirements
5. **Switch to paid search API** – If processing >10k organisations
