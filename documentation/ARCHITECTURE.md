# Pipeline Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    INPUT: CSV with org names                   │
│                  (organisation_name column)                    │
└─────────────────────────────────┬──────────────────────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────┐
                    │   Load into DataFrame   │
                    │      (pandas.read_csv)  │
                    └─────────────┬───────────┘
                                  │
                                  ▼
        ┌─────────────────────────────────────────────────┐
        │     For each organisation (async batch):        │
        └─────────────────────┬───────────────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │  Check CACHE?    │◄──────────────┐
                    └────┬────────┬────┘               │
                         │        │                     │
                    HIT  │        │  MISS               │
                         │        │                     │
                         │        ▼                     │
                         │  ┌──────────────────┐       │
                         │  │ STAGE 1:         │       │
                         │  │ Name Extraction  │       │
                         │  │ (regex patterns) │       │
                         │  └────┬─────────┬───┘       │
                         │       │         │           │
                         │  MATCH│         │NO MATCH   │
                         │       │         │           │
                         │       │         ▼           │
                         │       │   ┌──────────────┐  │
                         │       │   │ --offline?   │  │
                         │       │   └────┬─────┬───┘  │
                         │       │        │     │      │
                         │       │    YES │     │ NO   │
                         │       │        │     │      │
                         │       │        │     ▼      │
                         │       │        │  ┌─────────────────┐
                         │       │        │  │ STAGE 2a:       │
                         │       │        │  │ Batch Web LLM   │
                         │       │        │  │ (Gemini/OpenAI) │
                         │       │        │  └────┬───────┬────┘
                         │       │        │       │       │
                         │       │        │ SUCCESS │       │QUOTA EXHAUSTED
                         │       │        │       ▼       │
                         │       │        │  ┌─────────────────┐
                         │       │        │  │ URL Validation  │
                         │       │        │  │ (async httpx)   │
                         │       │        │  └────┬───────┬────┘
                         │       │        │       │       │
                         │       │        │HTTP 200/301   │DEAD LINK
                         │       │        │       │       │
                         │       │        ├───────┘       │
                         │       │        │               │
                         │       │        └───────┬───────┘
                         │       │                │
                         │       │                ▼
                         │       │         ┌──────────────────┐
                         │       │         │ STAGE 3:         │
                         │       │         │ Heuristics       │
                         │       │         │ (keywords)       │
                         │       │         └────┬───────┬─────┘
                         │       │              │       │
                         │       │         MATCH│       │NO MATCH
                         │       │              │       │
                         │       │              │       ▼
                         │       │              │  ┌──────────────┐
                         │       │              │  │ STAGE 4:     │
                         │       │              │  │ Unknown      │
                         │       │              │  │ (null)       │
                         │       │              │  └──────┬───────┘
                         │       │              │         │
                         ▼       ▼              ▼         ▼
                    ┌──────────────────────────────────────┐
                    │   ClassificationResult               │
                    │   • legal_form                       │
                    │   • confidence (high/medium/low/unk) │
                    │   • source (name/web/heuristic/null) │
                    └──────────────────┬───────────────────┘
                                       │
                                       │ Store in CACHE ───┘
                                       │
                                       ▼
                         ┌─────────────────────────┐
                         │  Collect all results    │
                         └──────────┬──────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │ STAGE 5: Final Sanitize Step  │
                    │  (Forces Canonical Names)     │
                    └──────────┬────────────────────┘
                               │
                               ▼
                    ┌───────────────────────────────┐
                    │  Merge with input DataFrame   │
                    └──────────┬────────────────────┘
                               │
                               ▼
                    ┌───────────────────────────────┐
                    │   Write OUTPUT CSV            │
                    │   + legal_form                │
                    │   + confidence                │
                    │   + source                    │
                    └───────────────────────────────┘
```

---

## Stage Details

### Stage 1: Name Extraction (`name_extractor.py`)

- Pattern matching against 30+ German legal forms
- Longest-first ordering (avoid false positives)
- Coverage: 60-70%
- Speed: Instant
- Confidence: High
- Source: "name"

### Stage 2: Batch LLM Web Search (`pipeline.py`)

- Batches up to 50 unknown organizations per API call
- Uses Google Gemini API as primary, OpenAI as fallback
- LLM searches the web and returns JSON with legal form and source URL
- Validates URLs asynchronously via HEAD/GET requests
- Coverage: +15-25%
- Speed: Very fast (due to batching and async architecture)
- Confidence: High/Medium (if validated) or Low (if dead link)
- Source: "web_search" or "openai"
- Skipped when: `--offline` flag set or quotas exhausted

### Stage 3: Heuristics (`heuristic.py`)

- Keyword matching (verein→e.V., stiftung→Stiftung, etc.)
- Conservative rules (low false positive rate)
- Runs as a final pass on remaining failures/unknowns
- Coverage: +5-10%
- Speed: Instant
- Confidence: Low
- Source: "heuristic"

### Stage 4: Unknown (`heuristic.py`)

- Fallback for unclassifiable cases
- legal_form: null
- confidence: "unknown"
- source: null

### Stage 5: Final Sanitize Step (`utils.py` & `pipeline.py`)

- Runs over all completed results just before writing the CSV
- Ensures formats like "eingetragener Verein" are standardized to "e.V."
- Prevents LLM hallucinations or variations from entering the final data

---

## Concurrency Control

```
┌──────────────────────────────────────────────┐
│  RateLimiter (Tokens & Requests)             │
│                                              │
│  ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐   │
│  │Batch│ │Batch│ │Batch│ │Batch│ │Batch│   │
│  └─────┘ └─────┘ └─────┘ └─────┘ └─────┘   │
│                                              │
│  ▲ Strict TPM (Tokens Per Minute) tracking   │
│  ▲ Graceful backoffs via Tenacity            │
└──────────────────────────────────────────────┘
```

---

## Caching Strategy

```
┌────────────────────────────────┐
│   diskcache (.cache/)          │
│                                │
│   Key: "org:siemens ag"        │
│   Value: {                     │
│     "legal_form": "AG",        │
│     "confidence": "high",      │
│     "source": "name"           │
│   }                            │
│                                │
│   ✓ Persists across runs       │
│   ✓ Normalized keys            │
│   ✓ Can be cleared             │
└────────────────────────────────┘
```

---

## Error Handling

```
┌──────────────────────────────────────┐
│  Tenacity Retry Logic                │
│                                      │
│  Web LLM Search:                     │
│    • Exponential backoff for 429     │
│    • Tracks exact API quota limits   │
│    • Early exit if completely empty  │
│                                      │
│  HTTP Validator:                     │
│    • 2 attempts                      │
│    • Follows redirects               │
│                                      │
│  On failure: Fall through to next    │
│  stage (graceful degradation)        │
└──────────────────────────────────────┘
```

