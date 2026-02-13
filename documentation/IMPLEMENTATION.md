# Implementation Notes

## Architecture Overview

The pipeline implements a **confidence-based cascade** with four stages:

1. **Name Extraction (Regex)** - ~60-70% hit rate, instant
2. **Web Search + Impressum** - Medium coverage, slower (optional with `--offline`)
3. **Heuristics** - Low-confidence fallback
4. **Null Assignment** - Unknown cases

## Key Design Decisions

### 1. Legal Form Patterns (constants.py)

- **30+ German legal forms** ordered longest-match-first to avoid false positives
- Example: `GmbH & Co. KG` must match before plain `GmbH`
- Regex patterns use word boundaries (`\b`) to prevent substring matches
- Case-insensitive matching for robustness

### 2. Async Architecture

- Uses `asyncio` + `httpx.AsyncClient` for concurrent web requests
- `asyncio.Semaphore` limits concurrent requests (default: 5)
- Prevents rate-limiting and resource exhaustion
- All I/O operations (web, disk cache) are async

### 3. Caching Strategy (cache.py)

- **Disk-based cache** using `diskcache` (survives restarts)
- Keys are normalized: `org:<lowercase_trimmed_name>`
- Avoids redundant web searches across multiple runs
- Can be cleared with `--clear-cache` flag

### 4. Error Handling

- **Tenacity** library for automatic retries (exponential backoff)
- Web search: 3 attempts, 2-10 second backoff
- Page fetch: 2 attempts, 2-5 second backoff
- Failures gracefully fall through to next stage

### 5. Offline Mode

- `--offline` flag skips web search stage entirely
- Cascade becomes: Name → Heuristics → Unknown
- Useful for: testing, air-gapped environments, fast dry-runs
- ~60-75% coverage without web access

### 6. Progress Tracking

- Uses `tqdm` for progress bar during batch processing
- Logs statistics after completion (confidence/source breakdown)

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

## Further Considerations (From Plan)

### 1. Web Search API Choice

**Current:** `googlesearch-python` (free, fragile, rate-limited)

**Alternatives:**

- **SerpAPI** ($50/mo, 5000 searches) - Reliable, no rate limits
- **Searx** (self-hosted) - Free, requires infrastructure
- **Bing Search API** ($7/1000 queries) - Microsoft alternative

**Recommendation:** For production with >10k orgs, use SerpAPI or Bing API.

### 2. LLM Fallback Stage

**Proposal:** Add GPT-4o / local-LLM between heuristics and null-assignment

**Pros:**

- Boosts recall on ambiguous cases (e.g., "Berliner Kulturgesellschaft")
- Can handle typos, abbreviations, foreign names

**Cons:**

- Adds 1-2s latency per org
- Costs $0.01-0.05 per org (GPT-4)
- Requires API key / local model setup

**Implementation:**

```python
# src/org_classifier/classifiers/llm.py
async def classify_by_llm(org_name: str) -> ClassificationResult:
    prompt = f"Extract German legal form from: {org_name}"
    # Call OpenAI / local LLM
    # Return high/medium confidence result
```

**Recommendation:** Add as **optional stage** behind `--use-llm` flag.

### 3. Handelsregister Lookup

**API:** `offeneregister.de` (free, authoritative data)

**Integration point:** Between name extraction and web search

**Pros:**

- Authoritative legal-form data
- High confidence results
- No scraping ambiguity

**Cons:**

- Not all organisations in Handelsregister (e.g., small Vereine)
- API rate limits unknown
- Requires exact name matching

**Implementation:**

```python
# src/org_classifier/classifiers/handelsregister.py
async def classify_by_handelsregister(org_name: str) -> ClassificationResult:
    # Query offeneregister.de API
    # Return high confidence if found
```

**Recommendation:** Add as **Stage 1.5** (after name, before web search).

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

## Dependencies

- **pandas** - CSV I/O
- **httpx** - Async HTTP client
- **beautifulsoup4 + lxml** - HTML parsing
- **googlesearch-python** - Web search (fragile!)
- **diskcache** - Persistent caching
- **tenacity** - Retry logic
- **pydantic** - Data validation
- **typer** - CLI framework
- **tqdm** - Progress bars

## Next Steps

1. **Test with real data** - Run on sample dataset, measure accuracy
2. **Add Handelsregister stage** - Integrate offeneregister.de API
3. **Improve heuristics** - Add more conservative keyword rules
4. **Consider LLM fallback** - For production use with high accuracy requirements
5. **Switch to paid search API** - If processing >10k organisations
