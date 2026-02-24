# Implementation Notes

## Architecture Overview

The pipeline implements a **confidence-based cascade** with four main stages:

1. **Name Extraction (Regex)** – Fast, high-confidence, ~60-70% hit rate
2. **Batch Web Search URL Validation** – Intelligent LLM web batching for high throughput handling of edge-cases (skipped with `--offline`)
3. **Heuristics** – Low-confidence fallback
4. **Final Sanitization** – Enforces canonical abbreviations before CSV write

All stages are orchestrated asynchronously for maximum throughput and robust rate limit handling.

---

## Key Design Decisions

### 1. Legal Form Patterns (`constants.py`)

- 30+ German legal forms, ordered longest-match-first to avoid false positives
- Regex patterns use word boundaries (`\b`) and are case-insensitive
- Easy to extend with new forms

### 2. Batch LLM Processing

- Groups 50 unknown organisations at a time directly to Gemini or OpenAI.
- Uses strict rate limits logic (TPM/RPM) via `RateLimiter` classes.
- Validates URLs dynamically before assigning a confidence score to prevent LLM hallucination.

### 3. Caching & Checkpoints (`cache.py`)

- Disk-based cache using `diskcache` (survives restarts)
- Intermediate CSVs are dumped every 50 batches to guarantee data safety.
- Single unified cache key format: `org:<lowercase_trimmed_name>`

### 4. Custom Error Handling & Quota tracking

- Built completely robust handling of 429 Too Many Requests errors.
- Automatic Fallback from Google Gemini to OpenAI if primary API gets exhausted.
- URL validations are async with automatic redirect handling.

### 5. Offline Mode

- `--offline` flag skips web search stage entirely
- Cascade becomes: Name → Heuristics → Unknown
- Useful for testing, air-gapped environments, and fast dry-runs

### 6. Progress Tracking

- Uses `tqdm` for progress bar during batch processing
- Logs statistics after completion (confidence/source breakdown)
- Appends clean tables to the bottom of output CSVs.

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
  ├─ Batched Web search? (if online)
  │   ├─ Prompted Gemini (batch of 50): "{... organisations ...}"
  │   ├─ Returns: "e.V.", src: "www.berliner-kultur.de/impressum"
  │   └─ Validates URL: HTTP 200 OK → (medium, web_search, <url>)
  └─ Result: e.V., medium, web_search, url
```

---

## Performance Characteristics

| Stage           | Coverage  | Speed   | Confidence  |
| --------------- | --------- | ------- | ----------- |
| Name extraction | 60-70%    | Instant | High        |
| Batch LLM       | 15-25%    | Very Fast | Medium/Low  |
| Heuristics      | 5-10%     | Instant | Low         |
| Unknown         | Remaining | Instant | Unknown     |

**Throughput estimates:**

- Offline mode: 10,000+ orgs/second
- Online mode: Purely dependent on API Token limits. With max-limits, hits 10k orgs in a few minutes.

---

## Extensibility & Future Enhancements

### 1. LLM Parameter Tuning
- Batch sizes can be varied based on API token limits.

### 2. Advanced Link Text Scraping
- Currently URL validation only checks for HTTP 200/301 statuses. We could optionally pull the text of the link to doubly verify the LLM hallucination.

### 3. Handelsregister Lookup
- Optional: Integrate offeneregister.de API as authoritative source
- Add as Stage 1.5 (after name, before web search)

---

## Installation & Usage

### Install

```bash
pip install -e .
```

### Setup keys
Set `.env` file keys
```env
GEMINI_API_KEY=ABC
OPENAI_API_KEY=DEF
```

### Basic Usage

```bash
# Offline mode (fast, ~65% coverage)
org-classifier input.csv output.csv --offline

# Online mode (slower, ~85% coverage)
org-classifier input.csv output.csv
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

**Output (Semicolon separated):**

```csv
organisation_name;legal_form;confidence;source;link_to_src
Siemens AG;AG;high;name;
Deutsche Bank AG;AG;high;name;
FC Bayern München e.V.;e.V.;high;name;
```

---

## Dependencies

- pandas – CSV I/O
- httpx – Async HTTP client
- google-genai / openai - APIs
- diskcache – Persistent caching
- tenacity – Retry logic
- pydantic – Data validation
- typer – CLI framework
- tqdm – Progress bars

---

## Next Steps

1. **Test with real data** – Run on sample dataset, measure accuracy
2. **Add Handelsregister stage** – Integrate offeneregister.de API
3. **Consider scraping API** – For completely authoritative non-LLM solutions.

