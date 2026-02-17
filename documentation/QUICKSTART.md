# Quick Start Guide

## Installation

```bash
# Install in development mode
pip install -e .

# Verify installation
org-classifier --version
```

## Basic Usage

### 1. Offline Mode (Fast, No Web Access)

Perfect for testing or when you only need regex-based classification.

```bash
org-classifier input.csv output.csv --offline

./.venv/bin/python -m org_classifier.main ./data/input_src.csv ./data/enriched_output.csv 
```

Coverage: ~60-70% (high confidence)
Speed: 10,000+ organisations/second

### 2. Online Mode (Full Pipeline)

Uses web search + Impressum scraping for better coverage.

```bash
org-classifier input.csv output.csv
```

Coverage: ~85-90% (high + medium confidence)
Speed: ~150-300 organisations/minute (rate-limited)

### 3. Custom Configuration

```bash
# Custom concurrency
org-classifier input.csv output.csv --max-workers 10

# Custom cache directory
org-classifier input.csv output.csv --cache-dir .mycache

# Custom column name
org-classifier input.csv output.csv --column company_name

# Example for German datasets
org-classifier input_src.csv enriched_output.csv --column Name_Organisation --offline

# Clear cache before processing
org-classifier input.csv output.csv --clear-cache
```

## Testing

Run the included test suite:

```bash
python test_pipeline.py
```

This will:

1. Test basic name extraction
2. Process the sample CSV
3. Test heuristic fallback
4. Display results and statistics

## CSV Format

Your input CSV must have a column with organisation names (default: `organisation_name`).

**Input example:**

```csv
organisation_name
Siemens AG
Deutsche Bank AG
FC Bayern München e.V.
REWE Markt GmbH
Stiftung Warentest
```

**Output example:**

```csv
organisation_name,legal_form,confidence,source
Siemens AG,AG,high,name
Deutsche Bank AG,AG,high,name
FC Bayern München e.V.,e.V.,high,name
REWE Markt GmbH,GmbH,high,name
Stiftung Warentest,Stiftung,high,name
```

## Confidence Levels

- **high** – Extracted from name or found in Impressum heading/title
- **medium** – Found in Impressum body text
- **low** – Heuristic keyword match
- **unknown** – No classification possible

## Source Types

- **name** – Extracted directly from organisation name
- **<URL>** – Found in Impressum at this URL
- **heuristic** – Conservative keyword matching
- **null** – No classification found

## Common Use Cases

### Large Dataset (100k+ organisations)

```bash
# Use offline mode first for quick wins
org-classifier large_input.csv output_phase1.csv --offline

# Then process only unknown cases with web search
# (manually filter output_phase1.csv for unknown results)
org-classifier unknowns.csv output_phase2.csv --max-workers 3
```

### No Internet Access

```bash
org-classifier input.csv output.csv --offline
```

### Reprocessing with Fresh Data

```bash
org-classifier input.csv output.csv --clear-cache
```

## Performance Tips

1. **Start with offline mode** – Gets 60-70% instantly
2. **Lower max-workers for web search** – Avoids rate limits (use 3-5)
3. **Use cache** – Don't clear it unless necessary
4. **Split large files** – Process in batches of 10-50k

## Troubleshooting

### "Column 'organisation_name' not found"

Use `--column` to specify your column name:

```bash
org-classifier input.csv output.csv --column company_name
```

### Rate limiting errors

Reduce concurrency:

```bash
org-classifier input.csv output.csv --max-workers 2
```

### Out of memory

Process in smaller batches or increase system RAM.

## Next Steps

- Read `IMPLEMENTATION.md` for architecture details
- Review `constants.py` for supported legal forms
- Consider adding Handelsregister API integration
- Evaluate LLM fallback for higher accuracy
