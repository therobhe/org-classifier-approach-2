# Pipeline Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    INPUT: CSV with org names                     │
│                  (organisation_name column)                      │
└─────────────────────────────────┬───────────────────────────────┘
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
                         │       │        │  │ STAGE 2:        │
                         │       │        │  │ Web Search      │
                         │       │        │  │ (Google)        │
                         │       │        │  └────┬───────┬────┘
                         │       │        │       │       │
                         │       │        │  FOUND│       │NOT FOUND
                         │       │        │       │       │
                         │       │        │       ▼       │
                         │       │        │  ┌─────────────────┐
                         │       │        │  │ Fetch Impressum │
                         │       │        │  │ (httpx + BS4)   │
                         │       │        │  └────┬───────┬────┘
                         │       │        │       │       │
                         │       │        │  MATCH│       │NO MATCH
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
                    │   • source (name/url/heuristic/null) │
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

## Stage Details

### Stage 1: Name Extraction (name_extractor.py)

- **Pattern matching** against 30+ German legal forms
- **Longest-first** ordering (avoid false positives)
- **Coverage:** 60-70%
- **Speed:** Instant
- **Confidence:** High
- **Source:** "name"

### Stage 2: Web Search + Impressum (web_search.py + impressum.py)

- **Google search** for "Organisation Impressum"
- **Fetch top 1-3 results** with httpx
- **Parse HTML** with BeautifulSoup
- **Extract legal forms** from text
- **Coverage:** +15-25%
- **Speed:** ~2 seconds per org
- **Confidence:** High (in title/headings) or Medium (in body)
- **Source:** Impressum URL
- **Skipped when:** `--offline` flag set

### Stage 3: Heuristics (heuristic.py)

- **Keyword matching** (verein→e.V., stiftung→Stiftung, etc.)
- **Conservative rules** (low false positive rate)
- **Coverage:** +5-10%
- **Speed:** Instant
- **Confidence:** Low
- **Source:** "heuristic"

### Stage 4: Unknown (heuristic.py)

- **Fallback** for unclassifiable cases
- **legal_form:** null
- **confidence:** "unknown"
- **source:** null

## Concurrency Control

```
┌──────────────────────────────────────────────┐
│  asyncio.Semaphore (max_concurrent_requests) │
│                                              │
│  ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐   │
│  │Req 1│ │Req 2│ │Req 3│ │Req 4│ │Req 5│   │
│  └─────┘ └─────┘ └─────┘ └─────┘ └─────┘   │
│                                              │
│  ▲ Maximum 5 concurrent web requests         │
│  ▲ Additional requests wait                  │
└──────────────────────────────────────────────┘
```

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

## Error Handling

```
┌──────────────────────────────────────┐
│  Tenacity Retry Logic                │
│                                      │
│  Web Search:                         │
│    • 3 attempts                      │
│    • Exponential backoff: 2-10s     │
│                                      │
│  HTTP Fetch:                         │
│    • 2 attempts                      │
│    • Exponential backoff: 2-5s      │
│                                      │
│  On failure: Fall through to next   │
│  stage (graceful degradation)       │
└──────────────────────────────────────┘
```
