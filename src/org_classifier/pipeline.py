import asyncio
import json
import random
import re
import httpx
import pandas as pd
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import logging
from tqdm import tqdm
import csv
from pandas.errors import ParserError

from .config import settings
from .models import ClassificationResult
from .cache import ClassificationCache
from .classifiers.name_extractor import classify_by_name
from .classifiers.heuristic import classify_by_heuristic, classify_as_unknown
from .party_rules import classify_party
from .utils import normalize_org_name

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

GEMINI_PROMPT_TEMPLATE = (
    'Find the legal form of the organization "{org_name}". '
    'Return only the following JSON: {{ "legal_form": "<FOUND_RESULT>", "src": "<URL>" }}. '
    'If unknown, use "unknown" for "legal_form" and "none" for "src". '
    'Do not invent values, do not hallucinate.'
)

logger = logging.getLogger(__name__)


def _pick_best_encoding(sample: bytes, candidates: List[str]) -> str:
    """Pick an encoding that yields the fewest replacement characters."""
    best = candidates[0]
    best_repl = float("inf")
    for enc in candidates:
        try:
            decoded = sample.decode(enc, errors="replace")
        except LookupError:
            continue
        repl = decoded.count("\ufffd")
        if repl < best_repl:
            best_repl = repl
            best = enc
            if repl == 0:
                break
    return best


def _sniff_dialect(sample_text: str) -> Tuple[str, str]:
    """Return (delimiter, quotechar) guessed from sample text."""
    delimiters = [";", ",", "\t", "|"]
    try:
        dialect = csv.Sniffer().sniff(sample_text, delimiters=delimiters)
        delimiter = dialect.delimiter
        quotechar = getattr(dialect, "quotechar", '"') or '"'
        return delimiter, quotechar
    except Exception:
        # Fallback heuristic: prefer ';' in German CSVs
        if sample_text.count(";") >= sample_text.count(","):
            return ";", '"'
        return ",", '"'


def _read_csv_smart(path: Path) -> pd.DataFrame:
    """Read CSV with delimiter/encoding detection and a ragged-row fallback."""
    sample = path.read_bytes()[:128_000]
    encoding = _pick_best_encoding(sample, ["utf-8-sig", "utf-8", "cp1252", "latin-1"])
    sample_text = sample.decode(encoding, errors="replace")
    delimiter, quotechar = _sniff_dialect(sample_text)

    try:
        df = pd.read_csv(
            path,
            sep=delimiter,
            encoding=encoding,
            engine="python",
            dtype=str,
            keep_default_na=False,
            quotechar=quotechar,
        )
        return df
    except ParserError:
        # Extremely defensive fallback: parse rows manually, pad/truncate to header length.
        rows: List[List[str]] = []
        with path.open("r", encoding=encoding, errors="replace", newline="") as f:
            reader = csv.reader(f, delimiter=delimiter, quotechar=quotechar)
            for row in reader:
                rows.append(["" if cell is None else str(cell) for cell in row])

        if not rows:
            return pd.DataFrame()

        header = rows[0]
        width = len(header)
        normalized_rows: List[List[str]] = [header]
        for row in rows[1:]:
            if len(row) < width:
                row = row + [""] * (width - len(row))
            elif len(row) > width:
                row = row[:width]
            normalized_rows.append(row)

        df = pd.DataFrame(normalized_rows[1:], columns=normalized_rows[0])
        return df


def _resolve_org_column(df: pd.DataFrame, requested: str) -> str:
    """Resolve organisation name column from common variants or fallback to first column."""
    if df.empty:
        return requested

    if requested in df.columns:
        return requested

    # Strip and case-insensitive matching
    normalized = {str(c).strip().lower(): c for c in df.columns}
    key = requested.strip().lower()
    if key in normalized:
        return str(normalized[key])

    # Common real-world variants
    for candidate in [
        "Name_Organisation",
        "Name_Organisation ",
        "Name_Organisation\ufeff",
        "name_organisation",
        "organisation",
        "org_name",
        "name",
    ]:
        cand_key = candidate.strip().lower()
        if cand_key in normalized:
            return str(normalized[cand_key])

    # Last resort: use the first column
    return str(df.columns[0])


class ClassificationPipeline:
    """Multi-stage classification pipeline for German legal forms."""
    
    def __init__(
        self,
        cache_dir: str = ".cache",
        max_concurrent_requests: int = 5,
        offline: bool = False,
        with_heuristic: bool = False
    ):
        self.cache = ClassificationCache(cache_dir)
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)
        self.offline = offline
        self.with_heuristic = with_heuristic
        self.http_client: Optional[httpx.AsyncClient] = None
        self._gemini_rate_lock = asyncio.Lock()
        self._last_gemini_request_ts = 0.0
        self._gemini_quota_exhausted = False
        self._openai_rate_lock = asyncio.Lock()
        self._last_openai_request_ts = 0.0
        self._openai_quota_exhausted = False
    
    async def __aenter__(self):
        """Async context manager entry."""
        if not self.offline:
            self.http_client = httpx.AsyncClient()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.http_client:
            await self.http_client.aclose()
        self.cache.close()
    
    async def classify_organisation_fast(self, org_name: str) -> Optional[ClassificationResult]:
        """
        Fast classification using only local methods (cache, regex, party rules).
        Returns None if the organisation could not be classified.
        
        This method is used in the first pass to quickly classify organisations
        without expensive web searches or API calls.
        """
        # Check cache first
        cached = self.cache.get(org_name)
        if cached:
            logger.debug(f"Cache hit for: {org_name}")
            return cached
        
        # Stage 1: Name extraction (regex)
        result = classify_by_name(org_name)
        if result:
            self.cache.set(result)
            return result
        
        # Party regex rules (high-confidence, local-only)
        try:
            party_match = classify_party(org_name)
        except Exception:
            party_match = None
        if party_match:
            result = ClassificationResult(
                organisation_name=org_name,
                legal_form=party_match.get("legal_form"),
                confidence=party_match.get("confidence", "high"),
                source=party_match.get("source", "name"),
            )
            self.cache.set(result)
            return result
        
        # Optional: Apply heuristics in the fast pass
        if self.with_heuristic:
            result = classify_by_heuristic(org_name)
            if result:
                self.cache.set(result)
                return result
        
        # Return None to indicate this org needs further processing
        return None
    
    async def _call_gemini(self, org_name: str) -> Optional[Dict[str, Any]]:
        """Call Gemini API and return parsed {"legal_form": ..., "src": ...} or None."""
        if self._gemini_quota_exhausted:
            return None

        api_key = settings.gemini_api_key
        if not api_key:
            logger.warning("GEMINI_API_KEY not configured – skipping Gemini classification")
            return None

        url = GEMINI_API_URL.format(model=settings.gemini_model)
        prompt_text = GEMINI_PROMPT_TEMPLATE.format(org_name=org_name)
        payload = {
            "contents": [{"parts": [{"text": prompt_text}]}],
        }
        headers = {"x-goog-api-key": api_key}
        max_retries = max(0, int(settings.gemini_max_retries))

        resp: Optional[httpx.Response] = None
        for attempt in range(max_retries + 1):
            try:
                async with self.semaphore:
                    await self._pace_gemini_requests()
                    resp = await self.http_client.post(
                        url,
                        headers=headers,
                        json=payload,
                        timeout=float(settings.gemini_timeout_seconds),
                    )
            except httpx.RequestError as exc:
                if attempt >= max_retries:
                    logger.warning(f"Gemini request failed for '{org_name}' after retries: {exc}")
                    return None
                delay = self._compute_backoff_delay(attempt=attempt)
                logger.info(
                    f"Gemini request error for '{org_name}', retrying in {delay:.1f}s "
                    f"({attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 429:
                response_text = (resp.text or "")[:800]
                lowered = response_text.lower()
                if (
                    "quota" in lowered
                    or "resource_exhausted" in lowered
                    or "rate limit exceeded" in lowered
                    or "billing" in lowered
                ):
                    self._gemini_quota_exhausted = True
                    logger.warning(
                        "Gemini quota appears exhausted (429/RESOURCE_EXHAUSTED). "
                        "Skipping further Gemini web classification for this run."
                    )
                    return None

                if attempt >= max_retries:
                    logger.warning(f"Gemini rate-limited for '{org_name}' after retries (429)")
                    return None
                delay = self._compute_backoff_delay(attempt=attempt, retry_after=resp.headers.get("Retry-After"))
                logger.info(
                    f"Gemini 429 for '{org_name}', backing off {delay:.1f}s "
                    f"({attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(delay)
                continue

            if 500 <= resp.status_code <= 599:
                if attempt >= max_retries:
                    logger.warning(
                        f"Gemini server error for '{org_name}' after retries "
                        f"(status={resp.status_code})"
                    )
                    return None
                delay = self._compute_backoff_delay(attempt=attempt)
                logger.info(
                    f"Gemini server error {resp.status_code} for '{org_name}', "
                    f"retrying in {delay:.1f}s ({attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(delay)
                continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    f"Gemini API HTTP error for '{org_name}' "
                    f"(status={resp.status_code}): {exc.response.text[:300]}"
                )
                return None
            break

        if resp is None:
            return None

        try:
            body = resp.json()
            raw_text = body["candidates"][0]["content"]["parts"][0]["text"]
            # Strip optional markdown fences (```json ... ```)
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
            cleaned = re.sub(r"```\s*$", "", cleaned.strip())
            parsed = json.loads(cleaned)
            if "legal_form" not in parsed:
                logger.warning(f"Gemini response missing 'legal_form' for '{org_name}'")
                return None
            return parsed
        except Exception as exc:
            logger.warning(f"Failed to parse Gemini response for '{org_name}': {exc}")
            return None

    async def _pace_gemini_requests(self) -> None:
        """Throttle Gemini calls to reduce 429 likelihood."""
        min_interval = max(0.0, float(settings.gemini_min_interval_seconds))
        if min_interval <= 0:
            return

        async with self._gemini_rate_lock:
            now = asyncio.get_running_loop().time()
            wait = (self._last_gemini_request_ts + min_interval) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_gemini_request_ts = asyncio.get_running_loop().time()

    async def _pace_openai_requests(self) -> None:
        """Throttle OpenAI calls to reduce 429 likelihood."""
        min_interval = max(0.0, float(settings.openai_min_interval_seconds))
        if min_interval <= 0:
            return

        async with self._openai_rate_lock:
            now = asyncio.get_running_loop().time()
            wait = (self._last_openai_request_ts + min_interval) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_openai_request_ts = asyncio.get_running_loop().time()

    async def _call_openai(self, org_name: str) -> Optional[Dict[str, Any]]:
        """Call OpenAI API and return parsed {"legal_form": ..., "src": ...} or None."""
        if self._openai_quota_exhausted:
            return None

        api_key = settings.openai_api_key
        if not api_key:
            return None

        prompt_text = GEMINI_PROMPT_TEMPLATE.format(org_name=org_name)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": settings.openai_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user", "content": prompt_text},
            ],
        }
        max_retries = max(0, int(settings.openai_max_retries))

        resp: Optional[httpx.Response] = None
        for attempt in range(max_retries + 1):
            try:
                async with self.semaphore:
                    await self._pace_openai_requests()
                    resp = await self.http_client.post(
                        OPENAI_API_URL,
                        headers=headers,
                        json=payload,
                        timeout=float(settings.openai_timeout_seconds),
                    )
            except httpx.RequestError as exc:
                if attempt >= max_retries:
                    logger.warning(f"OpenAI request failed for '{org_name}' after retries: {exc}")
                    return None
                delay = self._compute_backoff_delay(
                    attempt=attempt,
                    base=float(settings.openai_backoff_base_seconds),
                    cap=float(settings.openai_backoff_max_seconds),
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 429:
                response_text = (resp.text or "")[:800]
                lowered = response_text.lower()
                if "insufficient_quota" in lowered or "quota" in lowered or "billing" in lowered:
                    self._openai_quota_exhausted = True
                    logger.warning(
                        "OpenAI quota appears exhausted (429/insufficient_quota). "
                        "Skipping further OpenAI web classification for this run."
                    )
                    return None

                if attempt >= max_retries:
                    logger.warning(f"OpenAI rate-limited for '{org_name}' after retries (429)")
                    return None
                delay = self._compute_backoff_delay(
                    attempt=attempt,
                    retry_after=resp.headers.get("Retry-After"),
                    base=float(settings.openai_backoff_base_seconds),
                    cap=float(settings.openai_backoff_max_seconds),
                )
                await asyncio.sleep(delay)
                continue

            if 500 <= resp.status_code <= 599:
                if attempt >= max_retries:
                    logger.warning(
                        f"OpenAI server error for '{org_name}' after retries "
                        f"(status={resp.status_code})"
                    )
                    return None
                delay = self._compute_backoff_delay(
                    attempt=attempt,
                    base=float(settings.openai_backoff_base_seconds),
                    cap=float(settings.openai_backoff_max_seconds),
                )
                await asyncio.sleep(delay)
                continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    f"OpenAI API HTTP error for '{org_name}' "
                    f"(status={resp.status_code}): {exc.response.text[:300]}"
                )
                return None
            break

        if resp is None:
            return None

        try:
            body = resp.json()
            raw_text = body["choices"][0]["message"]["content"]
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
            cleaned = re.sub(r"```\s*$", "", cleaned.strip())
            parsed = json.loads(cleaned)
            if "legal_form" not in parsed:
                logger.warning(f"OpenAI response missing 'legal_form' for '{org_name}'")
                return None
            return parsed
        except Exception as exc:
            logger.warning(f"Failed to parse OpenAI response for '{org_name}': {exc}")
            return None

    def _compute_backoff_delay(
        self,
        attempt: int,
        retry_after: Optional[str] = None,
        base: Optional[float] = None,
        cap: Optional[float] = None,
    ) -> float:
        """Compute retry delay using Retry-After if present, else exponential backoff + jitter."""
        if retry_after:
            try:
                retry_seconds = float(retry_after.strip())
                if retry_seconds > 0:
                    return retry_seconds
            except Exception:
                pass

        resolved_base = max(
            0.1,
            float(base if base is not None else settings.gemini_backoff_base_seconds),
        )
        resolved_cap = max(
            resolved_base,
            float(cap if cap is not None else settings.gemini_backoff_max_seconds),
        )
        delay = min(resolved_cap, resolved_base * (2 ** max(0, attempt)))
        jitter = random.uniform(0.0, delay * 0.25)
        return min(resolved_cap, delay + jitter)

    async def _verify_url(self, url: str) -> bool:
        """Verify a URL is reachable via HTTP HEAD (returns True for 2xx)."""
        try:
            resp = await self.http_client.head(url, follow_redirects=True, timeout=10.0)
            return resp.is_success
        except Exception:
            return False

    async def classify_organisation_web(self, org_name: str) -> ClassificationResult:
        """
        Expensive classification using Gemini API.
        Only called for organisations that couldn't be classified by fast methods.

        Three-part flow:
        1) Send query to Gemini API.
        2) Read the response:
           - Case 1: legal_form is "unknown" / src is "none" → store as unknown.
           - Case 2: legal_form and src URL returned → verify the URL.
        3) Verify URL: if reachable → accept result; otherwise → treat as unknown.

        Returns a classification result (may be 'unknown' if Gemini or verification fails).
        """
        if not self.offline:
            # Part 1: Send query to Gemini, then fallback to OpenAI if needed
            llm_provider = "gemini"
            llm_result = await self._call_gemini(org_name)

            if llm_result is None:
                fallback_result = await self._call_openai(org_name)
                if fallback_result is not None:
                    llm_provider = "openai"
                    llm_result = fallback_result

            if llm_result is not None:
                legal_form = llm_result.get("legal_form", "unknown")
                src = llm_result.get("src", "none")

                # Part 2, Case 1: Gemini returned unknown / no link
                is_unknown = (
                    not legal_form
                    or str(legal_form).strip().lower() == "unknown"
                )
                has_no_link = (
                    not src
                    or str(src).strip().lower() == "none"
                )

                if is_unknown or has_no_link:
                    result = ClassificationResult(
                        organisation_name=org_name,
                        legal_form=legal_form if not is_unknown else None,
                        confidence="unknown",
                        source=llm_provider,
                        link_to_src=None,
                    )
                    self.cache.set(result)
                    return result

                # Part 2, Case 2: legal_form + src URL present → verify
                # Part 3: Verify the returned URL
                url_valid = await self._verify_url(src)

                if url_valid:
                    result = ClassificationResult(
                        organisation_name=org_name,
                        legal_form=legal_form,
                        confidence="medium",
                        source=llm_provider,
                        link_to_src=src,
                    )
                    self.cache.set(result)
                    return result
                else:
                    logger.info(
                        f"URL verification failed for '{org_name}' "
                        f"(url={src}) – marking as unknown"
                    )
                    result = ClassificationResult(
                        organisation_name=org_name,
                        legal_form=None,
                        confidence="unknown",
                        source=None,
                        link_to_src=None,
                    )
                    self.cache.set(result)
                    return result

        # Stage 3: Fallback to unknown
        result = classify_as_unknown(org_name)
        self.cache.set(result)
        return result
    
    async def classify_organisation(self, org_name: str) -> ClassificationResult:
        """
        Classify a single organisation through the full cascade.
        This is kept for backward compatibility and single-org classification.
        """
        # Try fast classification first
        result = await self.classify_organisation_fast(org_name)
        if result:
            return result
        
        # Fall back to web search
        return await self.classify_organisation_web(org_name)
    
    async def process_batch(
        self,
        org_names: List[str],
        show_progress: bool = True
    ) -> List[ClassificationResult]:
        """Process a batch of organisations using a two-pass approach.
        
        Pass 1: Fast local classification (cache + regex + party rules)
        Pass 2: Expensive web search/LLM only for unknowns
        
        This optimizes performance by avoiding unnecessary API calls for
        organisations that can be classified locally.
        """
        # Pass 1: Fast local classification
        logger.info(f"Pass 1: Fast classification of {len(org_names)} organisations...")
        classified_results = {}
        unknowns = []
        
        pbar = tqdm(total=len(org_names), desc="Fast classification") if show_progress else None
        
        for org_name in org_names:
            result = await self.classify_organisation_fast(org_name)
            if result:
                classified_results[org_name] = result
            else:
                unknowns.append(org_name)
            
            if pbar:
                pbar.update(1)
        
        if pbar:
            pbar.close()
        
        logger.info(f"Fast pass: {len(classified_results)} classified, {len(unknowns)} unknowns")
        
        # Pass 2: Web search for unknowns only (chunked + deduplicated)
        if unknowns and not self.offline:
            # Deduplicate: only query each unique name once
            unique_unknowns = list(dict.fromkeys(unknowns))
            duplicates_saved = len(unknowns) - len(unique_unknowns)
            if duplicates_saved:
                logger.info(
                    f"Deduplicated unknowns: {len(unknowns)} → {len(unique_unknowns)} "
                    f"({duplicates_saved} duplicate API calls avoided)"
                )

            total_unknowns = len(unique_unknowns)
            target_chunks = 25
            chunk_size = max(1, (total_unknowns + target_chunks - 1) // target_chunks)
            chunks = [unique_unknowns[i:i + chunk_size] for i in range(0, total_unknowns, chunk_size)]

            logger.info(
                f"Pass 2: Web search for {total_unknowns} unique unknowns "
                f"in {len(chunks)} chunks (chunk_size={chunk_size})..."
            )
            pbar = tqdm(total=total_unknowns, desc="Web classification") if show_progress else None

            web_results: Dict[str, ClassificationResult] = {}
            processed = 0
            quota_stop = False

            for chunk_idx, chunk in enumerate(chunks, start=1):
                # Early stop: both providers exhausted
                if self._gemini_quota_exhausted and self._openai_quota_exhausted:
                    quota_stop = True
                    logger.warning(
                        f"Pass 2 early stop before chunk {chunk_idx}/{len(chunks)}: "
                        f"both Gemini and OpenAI quota exhausted "
                        f"({processed}/{total_unknowns} processed)."
                    )
                    break

                logger.info(
                    f"Pass 2 chunk {chunk_idx}/{len(chunks)} start "
                    f"(size={len(chunk)}, processed={processed}/{total_unknowns})"
                )

                chunk_classified = 0
                for name in chunk:
                    # Check cache first to avoid redundant API calls on resume
                    cached = self.cache.get(name)
                    if cached:
                        web_results[name] = cached
                        processed += 1
                        chunk_classified += 1
                        if pbar:
                            pbar.update(1)
                        continue

                    result = await self.classify_organisation_web(name)
                    web_results[name] = result
                    processed += 1
                    chunk_classified += 1
                    if pbar:
                        pbar.update(1)

                    # Check quota after each call
                    if self._gemini_quota_exhausted and self._openai_quota_exhausted:
                        quota_stop = True
                        logger.warning(
                            f"Pass 2 early stop in chunk {chunk_idx}/{len(chunks)} after "
                            f"{chunk_classified}/{len(chunk)} in chunk "
                            f"({processed}/{total_unknowns} total)."
                        )
                        break

                logger.info(
                    f"Pass 2 chunk {chunk_idx}/{len(chunks)} done "
                    f"({chunk_classified}/{len(chunk)} classified; "
                    f"cumulative {processed}/{total_unknowns})"
                )

                if quota_stop:
                    break

            if pbar:
                pbar.close()

            # Map results back to all unknowns (including duplicates);
            # any unprocessed names (quota stop) fall back to unknown.
            for org_name in unknowns:
                classified_results[org_name] = web_results.get(
                    org_name, classify_as_unknown(org_name)
                )

            if quota_stop:
                remaining = total_unknowns - processed
                if remaining > 0:
                    logger.warning(
                        f"Pass 2 fallback: marked {remaining} remaining organisations "
                        f"as unknown due to quota exhaustion."
                    )
        else:
            # Offline mode or no unknowns: mark remaining as unknown
            for org_name in unknowns:
                result = classify_as_unknown(org_name)
                classified_results[org_name] = result
        
        # Preserve input order
        return [classified_results[name] for name in org_names]
    
    async def process_csv(
        self,
        input_path: Path,
        output_path: Path,
        org_column: str = "organisation_name"
    ) -> None:
        """
        Process a CSV file of organisations.
        
        Args:
            input_path: Path to input CSV with organisation names
            output_path: Path to write enriched CSV
            org_column: Name of column containing organisation names
        """
        # Read input CSV (robust delimiter/encoding detection)
        logger.info(f"Reading input CSV: {input_path}")
        df = _read_csv_smart(input_path)

        if df.empty:
            raise ValueError(f"Input CSV appears empty or unreadable: {input_path}")

        resolved_column = _resolve_org_column(df, org_column)
        if resolved_column != org_column:
            logger.info(f"Using organisation column: {resolved_column} (requested: {org_column})")

        # Normalize organisation names for matching
        org_names = [normalize_org_name(v) for v in df[resolved_column].tolist()]
        logger.info(f"Processing {len(org_names)} organisations")
        
        # Classify all organisations
        results = await self.process_batch(org_names)

        # Build a clean output with exactly 5 columns:
        #   organisation_name | legal_form | confidence | source | link_to_src
        # This avoids carrying over junk columns from semicolon-heavy inputs.
        output_df = pd.DataFrame(
            {
                "organisation_name": org_names,
                "legal_form": [r.legal_form for r in results],
                "confidence": [r.confidence for r in results],
                "source": [r.source for r in results],
                "link_to_src": [r.link_to_src for r in results],
            }
        )

        # Write output CSV (semicolon-separated + UTF-8 BOM for German Excel)
        logger.info(f"Writing output CSV: {output_path}")
        output_df.to_csv(output_path, index=False, encoding="utf-8-sig", sep=";")
        
        # Log statistics
        self._log_statistics(results)

        # Append the same statistics to the output CSV for downstream consumption.
        try:
            stats = self._compute_statistics(results)
            # Append a blank line then a machine-friendly semicolon-separated block.
            with output_path.open("a", encoding="utf-8-sig", newline="") as f:
                f.write("\n# Classification Statistics\n")
                f.write(f"Total;{stats['total']}\n")

                f.write("\nBy confidence;count;percent\n")
                for conf, count in stats["by_confidence"].items():
                    pct = (count / stats["total"]) * 100 if stats["total"] else 0.0
                    f.write(f"{conf};{count};{pct:.1f}%\n")

                f.write("\nBy source;count;percent\n")
                for src, count in stats["by_source"].items():
                    pct = (count / stats["total"]) * 100 if stats["total"] else 0.0
                    f.write(f"{src};{count};{pct:.1f}%\n")

                f.write("\nBy legal_form;count;percent\n")
                for lf, count in stats["forms_sorted"]:
                    pct = (count / stats["total"]) * 100 if stats["total"] else 0.0
                    f.write(f"{lf};{count};{pct:.1f}%\n")
        except Exception as e:
            logger.warning(f"Failed to append statistics to CSV: {e}")
    
    def _log_statistics(self, results: List[ClassificationResult]) -> None:
        """Log classification statistics."""
        stats = self._compute_statistics(results)
        total = stats["total"]

        logger.info(f"\nClassification Statistics:")
        logger.info(f"Total organisations: {total}")

        logger.info(f"\nBy confidence:")
        for conf, count in sorted(stats["by_confidence"].items()):
            pct = (count / total) * 100
            logger.info(f"  {conf}: {count} ({pct:.1f}%)")

        logger.info(f"\nBy source:")
        for src, count in sorted(stats["by_source"].items()):
            pct = (count / total) * 100
            logger.info(f"  {src}: {count} ({pct:.1f}%)")

        logger.info(f"\nBy legal form:")
        forms_sorted = stats["forms_sorted"]
        max_lines = 25
        other_count = 0
        for idx, (lf, count) in enumerate(forms_sorted):
            if idx >= max_lines:
                other_count += count
                continue
            pct = (count / total) * 100
            logger.info(f"  {lf}: {count} ({pct:.1f}%)")

        if other_count:
            pct = (other_count / total) * 100
            logger.info(f"  other: {other_count} ({pct:.1f}%)")

        # One-line summary for quick scanning
        top_k = 8
        summary_parts = []
        for lf, count in forms_sorted[:top_k]:
            pct = (count / total) * 100
            summary_parts.append(f"{lf} {pct:.1f}%")
        if len(forms_sorted) > top_k:
            summary_parts.append("...")
        logger.info("\nResume (top legal forms): " + ", ".join(summary_parts))

    def _compute_statistics(self, results: List[ClassificationResult]) -> dict:
        """Compute and return classification statistics for reuse and CSV export."""
        total = len(results)
        by_confidence = {}
        by_source = {}
        by_legal_form = {}

        for result in results:
            conf = result.confidence
            src = result.source or "unknown"
            lf = result.legal_form or "null"
            by_confidence[conf] = by_confidence.get(conf, 0) + 1
            by_source[src] = by_source.get(src, 0) + 1
            by_legal_form[lf] = by_legal_form.get(lf, 0) + 1

        forms_sorted = sorted(by_legal_form.items(), key=lambda kv: (-kv[1], str(kv[0])))

        return {
            "total": total,
            "by_confidence": by_confidence,
            "by_source": by_source,
            "by_legal_form": by_legal_form,
            "forms_sorted": forms_sorted,
        }
