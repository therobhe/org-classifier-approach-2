import asyncio
import json
import random
import re
from collections import deque
from datetime import date
from urllib.parse import quote_plus
import httpx
import pandas as pd
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import logging
from tqdm import tqdm
import csv
from pandas.errors import ParserError
from .cache import ClassificationCache
from .models import ClassificationResult
from .classifiers.name_extractor import classify_by_name
from .classifiers.heuristic import classify_by_heuristic, classify_as_unknown
from .party_rules import classify_party
from .utils import normalize_org_name, sanitize_legal_form
from .config import settings

# External API endpoints (can be overridden in future by config)
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

GEMINI_PROMPT_TEMPLATE = (
    'Find the legal form of the german organization "{org_name}".'
    ' Return only the following JSON: {{ "legal_form": "<FOUND_RESULT>", "src": "<URL>" }}.'
    ' If unknown, use "unknown" for "legal_form" and "none" for "src".'
    ' Do not invent values, do not hallucinate.'
)

GEMINI_BATCH_PROMPT_TEMPLATE = (
    'For each of the following German organizations, find their legal form (Rechtsform).\n'
    'Input (JSON array):\n{org_list_json}\n\n'
    'Return ONLY a JSON array where each element has: '
    '{{ "id": <INPUT_ID>, "legal_form": "<FOUND_RESULT>", "src": "<URL>" }}.\n'
    'If unknown, use "unknown" for "legal_form" and "none" for "src".\n'
    'Do not invent values, do not hallucinate.\n'
    'Return one result per input. Preserve the original "id" values.'
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
        self._openai_minute_request_timestamps = deque()
        self._openai_minute_token_events = deque()
        self._openai_daily_window_key = ""
        self._openai_requests_today = 0
        self._openai_tokens_today = 0
        self._openai_limits_logged = False
        self._inflight_web_lock = asyncio.Lock()
        self._inflight_web_requests: Dict[str, asyncio.Task] = {}
    
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
        # Apply Gemini generation tuning.
        try:
            media_level = str(settings.gemini_media_resolution or "low").strip().lower()
            media_map = {
                "low": "MEDIA_RESOLUTION_LOW",
                "medium": "MEDIA_RESOLUTION_MEDIUM",
                "high": "MEDIA_RESOLUTION_HIGH",
            }
            media_resolution = media_map.get(media_level, "MEDIA_RESOLUTION_LOW")

            thinking_level = str(settings.gemini_thinking_level or "low").strip().lower()
            thinking_budget_map = {
                "low": 0,
                "medium": 256,
                "high": 1024,
            }
            thinking_budget = thinking_budget_map.get(thinking_level, 0)

            payload["generationConfig"] = {
                "thinkingConfig": {"thinkingBudget": thinking_budget},
            }
        except Exception:
            # Conservative fallback: continue without generation tuning.
            pass
        # Optionally enable Google Search grounding for Gemini.
        try:
            if bool(settings.gemini_use_google_search_grounding):
                payload["tools"] = [{"google_search": {}}]
        except Exception:
            # Proceed without grounding if config parsing fails.
            pass
        # Optionally request structured JSON output from Gemini
        try:
            # Note: Gemini REST API currently does not support response_format/structured output
            # simultaneously with tool use (grounding). If grounding is enabled, we skip structured output.
            use_grounding = bool(settings.gemini_use_google_search_grounding)
            if bool(settings.gemini_use_structured_output) and not use_grounding:
                # Default schema: legal_form and src string fields
                if settings.gemini_structured_output_schema:
                    schema = json.loads(settings.gemini_structured_output_schema)
                else:
                    schema = {
                        "type": "OBJECT",
                        "properties": {
                            "legal_form": {"type": "STRING"},
                            "src": {"type": "STRING"},
                        },
                        "required": ["legal_form", "src"],
                    }

                if "generationConfig" not in payload:
                    payload["generationConfig"] = {}
                payload["generationConfig"]["responseMimeType"] = "application/json"
                payload["generationConfig"]["responseSchema"] = schema
        except Exception:
            # Proceed without structured output if schema parsing fails
            pass
        # Optionally include URL context for Gemini if configured.
        try:
            if bool(settings.gemini_use_url_context):
                raw = settings.gemini_url_context_urls or ""
                urls = [u.strip() for u in str(raw).split(",") if u and u.strip()]
                # Include an explicit 'context' section expected by the REST API.
                # The exact shape is conservative and should be adjusted if using
                # an SDK that requires a different key (e.g., google.genai types).
                payload["context"] = {"urlContext": {"urls": urls}}
        except Exception:
            # Conservative: if config parsing fails, proceed without URL context.
            pass
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
            grounded_url = self._extract_first_grounded_url(body)
            raw_text = body["candidates"][0]["content"]["parts"][0]["text"]
            # Strip optional markdown fences (```json ... ```)
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
            cleaned = re.sub(r"```\s*$", "", cleaned.strip())
            parsed = json.loads(cleaned)
            if "legal_form" not in parsed:
                logger.warning(f"Gemini response missing 'legal_form' for '{org_name}'")
                return None
            if grounded_url:
                parsed["src"] = grounded_url
            return parsed
        except Exception as exc:
            logger.warning(f"Failed to parse Gemini response for '{org_name}': {exc}")
            return None

    def _extract_first_grounded_url(self, body: Dict[str, Any]) -> Optional[str]:
        """Extract first grounded URL from Gemini response metadata if present."""
        try:
            candidates = body.get("candidates") or []
            if not candidates:
                return None

            grounding = candidates[0].get("groundingMetadata") or {}
            chunks = grounding.get("groundingChunks") or []
            for chunk in chunks:
                web = chunk.get("web") or {}
                uri = web.get("uri")
                if isinstance(uri, str) and uri.strip():
                    return uri.strip()

            supports = grounding.get("groundingSupports") or []
            for item in supports:
                segment = item.get("segment") or {}
                source_indices = segment.get("groundingChunkIndices") or []
                for idx in source_indices:
                    if isinstance(idx, int) and 0 <= idx < len(chunks):
                        web = (chunks[idx] or {}).get("web") or {}
                        uri = web.get("uri")
                        if isinstance(uri, str) and uri.strip():
                            return uri.strip()
        except Exception:
            return None
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

    def _estimate_text_tokens(self, text: str) -> int:
        """Rough token estimate using ~4 chars/token heuristic."""
        return max(1, (len(text or "") + 3) // 4)

    def _resolve_openai_limits(self) -> Tuple[int, int, int, int]:
        """Resolve effective OpenAI limits with model defaults and optional env overrides."""
        model = (settings.openai_model or "").strip().lower()
        defaults = {
            "gpt-5.1": (10_000, 3, 200, 900_000),
            "gpt-5-mini": (60_000, 3, 200, 200_000),
            "gpt-5-nano": (40_000, 3, 200, 200_000),
        }
        tpm, rpm, rpd, tpd = defaults.get(model, (40_000, 3, 200, 200_000))

        if settings.openai_tokens_per_minute is not None:
            tpm = int(settings.openai_tokens_per_minute)
        if settings.openai_requests_per_minute is not None:
            rpm = int(settings.openai_requests_per_minute)
        if settings.openai_requests_per_day is not None:
            rpd = int(settings.openai_requests_per_day)
        if settings.openai_tokens_per_day is not None:
            tpd = int(settings.openai_tokens_per_day)

        margin = min(1.0, max(0.1, float(settings.openai_quota_safety_margin)))
        tpm = max(1, int(tpm * margin))
        rpm = max(1, int(rpm * margin))
        rpd = max(1, int(rpd * margin))
        tpd = max(1, int(tpd * margin))
        return tpm, rpm, rpd, tpd

    def _reset_openai_daily_counters_if_needed(self) -> None:
        """Reset per-day counters when local date changes."""
        today_key = date.today().isoformat()
        if self._openai_daily_window_key != today_key:
            self._openai_daily_window_key = today_key
            self._openai_requests_today = 0
            self._openai_tokens_today = 0

    def _prune_openai_minute_windows(self, now_ts: float) -> None:
        """Drop request/token events older than 60 seconds."""
        cutoff = now_ts - 60.0
        while self._openai_minute_request_timestamps and self._openai_minute_request_timestamps[0] < cutoff:
            self._openai_minute_request_timestamps.popleft()
        while self._openai_minute_token_events and self._openai_minute_token_events[0][0] < cutoff:
            self._openai_minute_token_events.popleft()

    async def _wait_for_openai_capacity(self, estimated_total_tokens: int) -> bool:
        """Wait until OpenAI quotas allow a request; return False on hard daily/size limits."""
        if self._openai_quota_exhausted:
            return False

        tpm, rpm, rpd, tpd = self._resolve_openai_limits()
        if estimated_total_tokens > tpm:
            logger.warning(
                "OpenAI request estimate (%s tokens) exceeds effective TPM (%s). "
                "Reduce OPENAI_MAX_COMPLETION_TOKENS or prompt size.",
                estimated_total_tokens,
                tpm,
            )
            return False
        if estimated_total_tokens > tpd:
            logger.warning(
                "OpenAI request estimate (%s tokens) exceeds effective TPD (%s).",
                estimated_total_tokens,
                tpd,
            )
            return False

        if not self._openai_limits_logged:
            logger.info(
                "OpenAI limiter active for model '%s': TPM=%s RPM=%s RPD=%s TPD=%s (safety_margin=%.2f)",
                settings.openai_model,
                tpm,
                rpm,
                rpd,
                tpd,
                min(1.0, max(0.1, float(settings.openai_quota_safety_margin))),
            )
            self._openai_limits_logged = True

        wait_enabled = bool(settings.openai_wait_for_capacity_window)
        min_interval = max(0.0, float(settings.openai_min_interval_seconds))

        while True:
            now = asyncio.get_running_loop().time()
            self._reset_openai_daily_counters_if_needed()
            self._prune_openai_minute_windows(now)

            if self._openai_requests_today >= rpd:
                self._openai_quota_exhausted = True
                logger.warning(
                    "OpenAI daily request limit reached (%s/%s). Skipping further OpenAI calls for this run.",
                    self._openai_requests_today,
                    rpd,
                )
                return False

            if (self._openai_tokens_today + estimated_total_tokens) > tpd:
                self._openai_quota_exhausted = True
                logger.warning(
                    "OpenAI daily token limit reached (%s + %s > %s). "
                    "Skipping further OpenAI calls for this run.",
                    self._openai_tokens_today,
                    estimated_total_tokens,
                    tpd,
                )
                return False

            req_in_minute = len(self._openai_minute_request_timestamps)
            tok_in_minute = sum(tokens for _, tokens in self._openai_minute_token_events)

            interval_wait = max(0.0, (self._last_openai_request_ts + min_interval) - now)
            rpm_wait = 0.0
            if req_in_minute >= rpm:
                rpm_wait = max(0.0, (self._openai_minute_request_timestamps[0] + 60.0) - now)

            tpm_wait = 0.0
            if (tok_in_minute + estimated_total_tokens) > tpm:
                if self._openai_minute_token_events:
                    tpm_wait = max(0.0, (self._openai_minute_token_events[0][0] + 60.0) - now)
                else:
                    tpm_wait = 60.0

            required_wait = max(interval_wait, rpm_wait, tpm_wait)
            if required_wait <= 0.0:
                self._openai_minute_request_timestamps.append(now)
                self._openai_minute_token_events.append((now, estimated_total_tokens))
                self._openai_requests_today += 1
                self._openai_tokens_today += estimated_total_tokens
                self._last_openai_request_ts = now
                return True

            if not wait_enabled:
                logger.warning(
                    "OpenAI capacity window exceeded (RPM/TPM). "
                    "Set OPENAI_WAIT_FOR_CAPACITY_WINDOW=true to queue instead of skipping."
                )
                return False

            await asyncio.sleep(max(0.05, min(required_wait, 5.0)))

    def _release_openai_reserved_tokens(self) -> None:
        """Release last reserved OpenAI token event (used for failed/non-billed attempts)."""
        if not self._openai_minute_token_events:
            return
        _, reserved = self._openai_minute_token_events.pop()
        self._openai_tokens_today = max(0, self._openai_tokens_today - reserved)

    def _adjust_last_openai_reserved_tokens(self, actual_total_tokens: int) -> None:
        """Adjust last reserved OpenAI token event to actual usage if available."""
        if actual_total_tokens <= 0 or not self._openai_minute_token_events:
            return
        ts, reserved = self._openai_minute_token_events.pop()
        self._openai_minute_token_events.append((ts, actual_total_tokens))
        delta = actual_total_tokens - reserved
        self._openai_tokens_today = max(0, self._openai_tokens_today + delta)

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
            "max_tokens": max(1, int(settings.openai_max_completion_tokens)),
            "messages": [
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user", "content": prompt_text},
            ],
        }
        estimated_prompt_tokens = (
            self._estimate_text_tokens("Return only valid JSON.")
            + self._estimate_text_tokens(prompt_text)
        )
        estimated_total_tokens = (
            estimated_prompt_tokens + max(1, int(settings.openai_max_completion_tokens))
        )
        max_retries = max(0, int(settings.openai_max_retries))

        resp: Optional[httpx.Response] = None
        for attempt in range(max_retries + 1):
            try:
                async with self._openai_rate_lock:
                    has_capacity = await self._wait_for_openai_capacity(estimated_total_tokens)
                    if not has_capacity:
                        return None

                    async with self.semaphore:
                        resp = await self.http_client.post(
                            OPENAI_API_URL,
                            headers=headers,
                            json=payload,
                            timeout=float(settings.openai_timeout_seconds),
                        )

                    if resp.status_code >= 400:
                        self._release_openai_reserved_tokens()
                    else:
                        try:
                            usage = (resp.json() or {}).get("usage") or {}
                            actual_total_tokens = int(usage.get("total_tokens") or 0)
                            self._adjust_last_openai_reserved_tokens(actual_total_tokens)
                        except Exception:
                            # Keep reserved estimate if usage is unavailable.
                            pass
            except httpx.RequestError as exc:
                async with self._openai_rate_lock:
                    self._release_openai_reserved_tokens()
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

    @staticmethod
    def _google_search_url(org_name: str) -> str:
        """Build a Google search URL for the given organisation name + Rechtsform."""
        query = f"{org_name} Rechtsform"
        return f"https://www.google.com/search?q={quote_plus(query)}"

    async def _verify_url(self, url: str) -> Tuple[bool, Optional[str], bool]:
        """Verify URL reachability and return (is_valid, resolved_url, had_301_redirect)."""
        try:
            # We use GET because many servers and CDNs (Cloudflare, etc.) block HEAD requests
            resp = await self.http_client.get(url, follow_redirects=True, timeout=10.0)
            had_301 = any(r.status_code == 301 for r in resp.history)
            is_valid = resp.status_code == 200
            resolved_url = str(resp.url) if resp.url is not None else None
            return is_valid, resolved_url, had_301
        except Exception:
            return False, None, False


    async def _call_gemini_batch(self, org_names: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        """Call Gemini API with a batch of org names and return a dict mapping org_name -> parsed result."""
        if self._gemini_quota_exhausted:
            return {}

        api_key = settings.gemini_api_key
        if not api_key:
            logger.warning("GEMINI_API_KEY not configured – skipping Gemini batch classification")
            return {}

        # Build the indexed input list
        org_list = [{"id": i, "name": name} for i, name in enumerate(org_names)]
        org_list_json = json.dumps(org_list, ensure_ascii=False)

        url = GEMINI_API_URL.format(model=settings.gemini_model)
        prompt_text = GEMINI_BATCH_PROMPT_TEMPLATE.format(org_list_json=org_list_json)
        payload = {
            "contents": [{"parts": [{"text": prompt_text}]}],
        }
        # Apply Gemini generation tuning.
        try:
            media_level = str(settings.gemini_media_resolution or "low").strip().lower()
            media_map = {
                "low": "MEDIA_RESOLUTION_LOW",
                "medium": "MEDIA_RESOLUTION_MEDIUM",
                "high": "MEDIA_RESOLUTION_HIGH",
            }
            media_resolution = media_map.get(media_level, "MEDIA_RESOLUTION_LOW")

            thinking_level = str(settings.gemini_thinking_level or "low").strip().lower()
            thinking_budget_map = {
                "low": 0,
                "medium": 256,
                "high": 1024,
            }
            thinking_budget = thinking_budget_map.get(thinking_level, 0)

            payload["generationConfig"] = {
                "thinkingConfig": {"thinkingBudget": thinking_budget},
            }
        except Exception:
            pass

        # Optionally enable Google Search grounding.
        try:
            if bool(settings.gemini_use_google_search_grounding):
                payload["tools"] = [{"google_search": {}}]
        except Exception:
            pass

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
                        timeout=max(60.0, float(settings.gemini_timeout_seconds) * 2),
                    )
            except httpx.RequestError as exc:
                if attempt >= max_retries:
                    logger.warning(f"Gemini batch request failed after retries: {exc}")
                    return {}
                delay = self._compute_backoff_delay(attempt=attempt)
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
                        "Skipping further Gemini batch classification for this run."
                    )
                    return {}

                if attempt >= max_retries:
                    logger.warning("Gemini batch rate-limited after retries (429)")
                    return {}
                delay = self._compute_backoff_delay(
                    attempt=attempt, retry_after=resp.headers.get("Retry-After")
                )
                await asyncio.sleep(delay)
                continue

            if 500 <= resp.status_code <= 599:
                if attempt >= max_retries:
                    logger.warning(
                        f"Gemini batch server error after retries (status={resp.status_code})"
                    )
                    return {}
                delay = self._compute_backoff_delay(attempt=attempt)
                await asyncio.sleep(delay)
                continue

            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    f"Gemini batch API HTTP error (status={resp.status_code}): "
                    f"{exc.response.text[:300]}"
                )
                return {}
            break

        if resp is None:
            return {}

        # Parse batch response
        try:
            body = resp.json()
            raw_text = body["candidates"][0]["content"]["parts"][0]["text"]
            # Strip optional markdown fences
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
            cleaned = re.sub(r"```\s*$", "", cleaned.strip())
            parsed = json.loads(cleaned)

            if not isinstance(parsed, list):
                logger.warning("Gemini batch response is not a JSON array")
                return {}

            results: Dict[str, Optional[Dict[str, Any]]] = {}
            for item in parsed:
                try:
                    item_id = int(item.get("id", -1))
                    if 0 <= item_id < len(org_names):
                        org_name = org_names[item_id]
                        results[org_name] = {
                            "legal_form": item.get("legal_form", "unknown"),
                            "src": item.get("src", "none"),
                        }
                except (ValueError, TypeError, AttributeError):
                    continue

            logger.info(
                f"Gemini batch: parsed {len(results)}/{len(org_names)} results"
            )
            return results

        except Exception as exc:
            logger.warning(f"Failed to parse Gemini batch response: {exc}")
            return {}

    async def _call_openai_batch(self, org_names: List[str]) -> Dict[str, Optional[Dict[str, Any]]]:
        """Call OpenAI API with a batch of org names and return a dict mapping org_name -> parsed result."""
        if self._openai_quota_exhausted:
            return {}

        api_key = settings.openai_api_key
        if not api_key:
            return {}

        # Build the indexed input list
        org_list = [{"id": i, "name": name} for i, name in enumerate(org_names)]
        org_list_json = json.dumps(org_list, ensure_ascii=False)
        prompt_text = GEMINI_BATCH_PROMPT_TEMPLATE.format(org_list_json=org_list_json)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        # Scale max tokens for batch size
        single_max_tokens = max(1, int(settings.openai_max_completion_tokens))
        batch_max_tokens = min(single_max_tokens * len(org_names), 16_000)
        payload = {
            "model": settings.openai_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": batch_max_tokens,
            "messages": [
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user", "content": prompt_text},
            ],
        }

        estimated_prompt_tokens = (
            self._estimate_text_tokens("Return only valid JSON.")
            + self._estimate_text_tokens(prompt_text)
        )
        estimated_total_tokens = estimated_prompt_tokens + batch_max_tokens
        max_retries = max(0, int(settings.openai_max_retries))

        resp: Optional[httpx.Response] = None
        for attempt in range(max_retries + 1):
            try:
                async with self._openai_rate_lock:
                    has_capacity = await self._wait_for_openai_capacity(estimated_total_tokens)
                    if not has_capacity:
                        return {}

                    async with self.semaphore:
                        resp = await self.http_client.post(
                            OPENAI_API_URL,
                            headers=headers,
                            json=payload,
                            timeout=max(60.0, float(settings.openai_timeout_seconds) * 2),
                        )

                    if resp.status_code >= 400:
                        self._release_openai_reserved_tokens()
                    else:
                        try:
                            usage = (resp.json() or {}).get("usage") or {}
                            actual_total_tokens = int(usage.get("total_tokens") or 0)
                            self._adjust_last_openai_reserved_tokens(actual_total_tokens)
                        except Exception:
                            pass
            except httpx.RequestError as exc:
                async with self._openai_rate_lock:
                    self._release_openai_reserved_tokens()
                if attempt >= max_retries:
                    logger.warning(f"OpenAI batch request failed after retries: {exc}")
                    return {}
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
                        "OpenAI quota appears exhausted (429). "
                        "Skipping further OpenAI batch classification for this run."
                    )
                    return {}
                if attempt >= max_retries:
                    logger.warning("OpenAI batch rate-limited after retries (429)")
                    return {}
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
                        f"OpenAI batch server error after retries (status={resp.status_code})"
                    )
                    return {}
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
                    f"OpenAI batch API HTTP error (status={resp.status_code}): "
                    f"{exc.response.text[:300]}"
                )
                return {}
            break

        if resp is None:
            return {}

        # Parse batch response
        try:
            body = resp.json()
            raw_text = body["choices"][0]["message"]["content"]
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
            cleaned = re.sub(r"```\s*$", "", cleaned.strip())
            parsed = json.loads(cleaned)

            # OpenAI with response_format json_object may wrap in {"results": [...]}
            if isinstance(parsed, dict):
                for key in ("results", "organizations", "data", "items"):
                    if key in parsed and isinstance(parsed[key], list):
                        parsed = parsed[key]
                        break

            if not isinstance(parsed, list):
                logger.warning("OpenAI batch response is not a JSON array")
                return {}

            results: Dict[str, Optional[Dict[str, Any]]] = {}
            for item in parsed:
                try:
                    item_id = int(item.get("id", -1))
                    if 0 <= item_id < len(org_names):
                        org_name = org_names[item_id]
                        results[org_name] = {
                            "legal_form": item.get("legal_form", "unknown"),
                            "src": item.get("src", "none"),
                        }
                except (ValueError, TypeError, AttributeError):
                    continue

            logger.info(
                f"OpenAI batch: parsed {len(results)}/{len(org_names)} results"
            )
            return results

        except Exception as exc:
            logger.warning(f"Failed to parse OpenAI batch response: {exc}")
            return {}

    def _batch_result_to_classification(
        self, org_name: str, llm_result: Dict[str, Any], provider: str
    ) -> ClassificationResult:
        """Convert a single LLM batch result dict into a ClassificationResult."""
        legal_form = sanitize_legal_form(llm_result.get("legal_form", "unknown"))
        src = llm_result.get("src", "none")
        verification_failed = bool(llm_result.get("_verification_failed"))

        is_unknown = not legal_form or str(legal_form).strip().lower() == "unknown"
        has_no_link = not src or str(src).strip().lower() == "none"

        if is_unknown or has_no_link:
            return ClassificationResult(
                organisation_name=org_name,
                legal_form=legal_form if not is_unknown else None,
                confidence="unknown",
                source=provider,
                link_to_src=None,
            )

        return ClassificationResult(
            organisation_name=org_name,
            legal_form=legal_form,
            confidence="low" if verification_failed else "medium",
            source=provider,
            link_to_src=src,
        )

    async def classify_organisation_web(self, org_name: str) -> ClassificationResult:
        """Classify via web with strict cache + in-flight de-duplication."""
        # Strict cache check before any network call.
        cached = self.cache.get(org_name)
        if cached:
            return cached

        request_key = org_name.strip().lower()
        created_task = False

        async with self._inflight_web_lock:
            task = self._inflight_web_requests.get(request_key)
            if task is None:
                task = asyncio.create_task(self._classify_organisation_web_uncached(org_name))
                self._inflight_web_requests[request_key] = task
                created_task = True

        try:
            return await task
        finally:
            if created_task:
                async with self._inflight_web_lock:
                    if self._inflight_web_requests.get(request_key) is task:
                        self._inflight_web_requests.pop(request_key, None)

    async def _classify_organisation_web_uncached(self, org_name: str) -> ClassificationResult:
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
            llm_provider = "web_search"
            llm_result = await self._call_gemini(org_name)

            if llm_result is None:
                fallback_result = await self._call_openai(org_name)
                if fallback_result is not None:
                    llm_provider = "openai"
                    llm_result = fallback_result

            if llm_result is not None:
                legal_form = sanitize_legal_form(llm_result.get("legal_form", "unknown"))
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

                # Part 2, Case 2: legal_form + src URL present → verify URL
                is_valid, resolved_url, had_301 = await self._verify_url(src)

                # Part 3: Return result with validated URL
                if is_valid:
                    validated_src = resolved_url or src
                    if had_301 and validated_src != src:
                        logger.info(
                            f"URL redirect (301) for '{org_name}': '{src}' -> '{validated_src}'"
                        )
                    # Green highlight for successful validation
                    print(f"\033[92m✔ URL verified for '{org_name}': {validated_src}\033[0m")
                    result = ClassificationResult(
                        organisation_name=org_name,
                        legal_form=legal_form,
                        confidence="medium",
                        source=llm_provider,
                        link_to_src=validated_src,
                    )
                else:
                    google_url = self._google_search_url(org_name)
                    logger.info(
                        f"URL validation failed for '{org_name}' (URL: {src}). "
                        f"Falling back to Google search: {google_url}"
                    )
                    result = ClassificationResult(
                        organisation_name=org_name,
                        legal_form=legal_form,
                        confidence="low",
                        source=llm_provider,
                        link_to_src=google_url,
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
    
    def _write_checkpoint_csv(
        self,
        output_path: Path,
        all_org_names: List[str],
        classified_results: Dict[str, ClassificationResult],
        web_results: Dict[str, ClassificationResult],
        unknowns: List[str],
        batch_idx: int,
        num_batches: int,
    ) -> None:
        """Write an intermediate checkpoint CSV with all results so far."""
        try:
            # Merge fast-pass results with web results collected so far
            merged = dict(classified_results)
            for org_name in unknowns:
                if org_name in web_results:
                    merged[org_name] = web_results[org_name]
                elif org_name not in merged:
                    merged[org_name] = classify_as_unknown(org_name)

            # Build ordered results matching the original input order
            ordered_results = [
                merged.get(name, classify_as_unknown(name))
                for name in all_org_names
            ]

            checkpoint_df = pd.DataFrame(
                {
                    "organisation_name": all_org_names,
                    "legal_form": [r.legal_form for r in ordered_results],
                    "confidence": [r.confidence for r in ordered_results],
                    "source": [r.source for r in ordered_results],
                    "link_to_src": [r.link_to_src for r in ordered_results],
                }
            )

            checkpoint_df.to_csv(
                output_path, index=False, encoding="utf-8-sig", sep=";"
            )
            logger.info(
                f"Checkpoint CSV saved after batch {batch_idx}/{num_batches}: "
                f"{output_path}"
            )
        except Exception as exc:
            logger.warning(f"Failed to write checkpoint CSV: {exc}")

    async def process_batch(
        self,
        org_names: List[str],
        show_progress: bool = True,
        output_path: Optional[Path] = None,
        all_org_names: Optional[List[str]] = None,
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
        
        # Pass 2: Batch web search for unknowns (batch_size orgs per API call)
        if unknowns and not self.offline:
            # Deduplicate: only query each unique name once
            unique_unknowns = list(dict.fromkeys(unknowns))
            duplicates_saved = len(unknowns) - len(unique_unknowns)
            if duplicates_saved:
                logger.info(
                    f"Deduplicated unknowns: {len(unknowns)} → {len(unique_unknowns)} "
                    f"({duplicates_saved} duplicate API calls avoided)"
                )

            # Filter out any that are already cached (e.g. from a resumed run)
            uncached_unknowns = []
            web_results: Dict[str, ClassificationResult] = {}
            for name in unique_unknowns:
                cached = self.cache.get(name)
                if cached:
                    web_results[name] = cached
                else:
                    uncached_unknowns.append(name)

            if web_results:
                logger.info(
                    f"Pass 2 cache pre-filter: {len(web_results)} already cached, "
                    f"{len(uncached_unknowns)} need API calls"
                )

            total_to_process = len(uncached_unknowns)
            batch_size = max(1, int(settings.web_batch_size))
            batches = [
                uncached_unknowns[i:i + batch_size]
                for i in range(0, total_to_process, batch_size)
            ]
            num_batches = len(batches)

            logger.info(
                f"Pass 2: Batch web search for {total_to_process} unknowns "
                f"in {num_batches} batches (batch_size={batch_size})..."
            )
            pbar = tqdm(total=total_to_process, desc="Web classification (batch)") if show_progress else None

            processed = 0
            quota_stop = False

            for batch_idx, batch in enumerate(batches, start=1):
                # Early stop: both providers exhausted
                if self._gemini_quota_exhausted and self._openai_quota_exhausted:
                    quota_stop = True
                    logger.warning(
                        f"Pass 2 early stop before batch {batch_idx}/{num_batches}: "
                        f"both Gemini and OpenAI quota exhausted "
                        f"({processed}/{total_to_process} processed)."
                    )
                    break

                logger.info(
                    f"Pass 2 batch {batch_idx}/{num_batches} "
                    f"(size={len(batch)}, processed={processed}/{total_to_process})"
                )

                # Try Gemini batch first
                gemini_results = await self._call_gemini_batch(batch)

                # Collect orgs that Gemini resolved and validate URLs concurrently
                gemini_resolved = set()
                
                # Prepare tasks to validate URLs
                validation_tasks = {}
                for name in batch:
                    if name in gemini_results and gemini_results[name] is not None:
                        src = gemini_results[name].get("src")
                        if src and src.lower() != "none" and src.lower() != "unknown":
                            validation_tasks[name] = asyncio.create_task(self._verify_url(src))
                
                if validation_tasks:
                    await asyncio.gather(*validation_tasks.values(), return_exceptions=True)

                for name in batch:
                    if name in gemini_results and gemini_results[name] is not None:
                        is_valid = True
                        src = gemini_results[name].get("src")
                        
                        if name in validation_tasks:
                            try:
                                is_valid, resolved_url, had_301 = validation_tasks[name].result()
                                if is_valid:
                                    validated_src = resolved_url or src
                                    if had_301 and validated_src != src:
                                        logger.info(
                                            f"Gemini URL redirect (301) for '{name}': "
                                            f"'{src}' -> '{validated_src}'"
                                        )
                                    gemini_results[name]["src"] = validated_src
                                    print(f"\033[92m✔ URL verified for '{name}': {validated_src}\033[0m")
                                else:
                                    google_url = self._google_search_url(name)
                                    logger.info(
                                        f"Gemini URL validation failed for '{name}' (URL: {src}). "
                                        f"Falling back to Google search: {google_url}"
                                    )
                                    gemini_results[name]["src"] = google_url
                                    gemini_results[name]["_verification_failed"] = True
                            except Exception:
                                is_valid = False
                                google_url = self._google_search_url(name)
                                logger.info(f"Gemini URL validation raised exception for '{name}'. Falling back to Google search.")
                                gemini_results[name]["src"] = google_url
                                gemini_results[name]["_verification_failed"] = True

                        result = self._batch_result_to_classification(
                            name, gemini_results[name], "web_search"
                        )
                        web_results[name] = result
                        self.cache.set(result)
                        gemini_resolved.add(name)

                # Fallback: orgs not resolved by Gemini → try OpenAI batch
                gemini_failed = [n for n in batch if n not in gemini_resolved]
                if gemini_failed and not self._openai_quota_exhausted:
                    openai_results = await self._call_openai_batch(gemini_failed)
                    
                    # Prepare tasks to validate URLs for OpenAI
                    openai_validation_tasks = {}
                    for name in gemini_failed:
                        if name in openai_results and openai_results[name] is not None:
                            src = openai_results[name].get("src")
                            if src and src.lower() != "none" and src.lower() != "unknown":
                                openai_validation_tasks[name] = asyncio.create_task(self._verify_url(src))
                    
                    if openai_validation_tasks:
                        await asyncio.gather(*openai_validation_tasks.values(), return_exceptions=True)

                    for name in gemini_failed:
                        if name in openai_results and openai_results[name] is not None:
                            is_valid = True
                            src = openai_results[name].get("src")
                            
                            if name in openai_validation_tasks:
                                try:
                                    is_valid, resolved_url, had_301 = openai_validation_tasks[name].result()
                                    if is_valid:
                                        validated_src = resolved_url or src
                                        if had_301 and validated_src != src:
                                            logger.info(
                                                f"OpenAI URL redirect (301) for '{name}': "
                                                f"'{src}' -> '{validated_src}'"
                                            )
                                        openai_results[name]["src"] = validated_src
                                        print(f"\033[92m✔ URL verified for '{name}': {validated_src}\033[0m")
                                    else:
                                        google_url = self._google_search_url(name)
                                        logger.info(
                                            f"OpenAI URL validation failed for '{name}' (URL: {src}). "
                                            f"Falling back to Google search: {google_url}"
                                        )
                                        openai_results[name]["src"] = google_url
                                        openai_results[name]["_verification_failed"] = True
                                except Exception:
                                    is_valid = False
                                    google_url = self._google_search_url(name)
                                    logger.info(f"OpenAI URL validation raised exception for '{name}'. Falling back to Google search.")
                                    openai_results[name]["src"] = google_url
                                    openai_results[name]["_verification_failed"] = True

                            result = self._batch_result_to_classification(
                                name, openai_results[name], "openai"
                            )
                            web_results[name] = result
                            self.cache.set(result)

                # Any remaining unresolved → mark as unknown
                for name in batch:
                    if name not in web_results:
                        result = classify_as_unknown(name)
                        web_results[name] = result
                        self.cache.set(result)

                processed += len(batch)
                if pbar:
                    pbar.update(len(batch))

                logger.info(
                    f"Pass 2 batch {batch_idx}/{num_batches} done "
                    f"(cumulative {processed}/{total_to_process})"
                )

                # Checkpoint: write intermediate CSV every 50 batches
                if output_path and batch_idx % 50 == 0:
                    self._write_checkpoint_csv(
                        output_path=output_path,
                        all_org_names=all_org_names or org_names,
                        classified_results=classified_results,
                        web_results=web_results,
                        unknowns=unknowns,
                        batch_idx=batch_idx,
                        num_batches=num_batches,
                    )

                # Check quota after batch
                if self._gemini_quota_exhausted and self._openai_quota_exhausted:
                    quota_stop = True
                    logger.warning(
                        f"Pass 2 early stop after batch {batch_idx}/{num_batches}: "
                        f"both quotas exhausted ({processed}/{total_to_process} processed)."
                    )
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
                remaining = total_to_process - processed
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
        results = await self.process_batch(
            org_names,
            output_path=output_path,
            all_org_names=org_names,
        )

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

def remove_estimated_forms(input_path: Path, output_path: Path) -> None:
    """
    Standalone mode: reads a previously classified CSV, removes the legal_form 
    if confidence is 'unknown', and writes out the updated CSV with correct statistics.
    """
    import io
    
    logger.info(f"Running standalone --no-estimated-form mode on {input_path}")
    
    lines = []
    with input_path.open("r", encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            if line.startswith("# Classification Statistics"):
                break
            if line.strip() == "":
                continue
            lines.append(line)
            
    if not lines:
        raise ValueError(f"Input file is empty or invalid: {input_path}")
        
    csv_content = "".join(lines)
    
    delimiter, quotechar = _sniff_dialect(csv_content[:8192])
    
    df = pd.read_csv(io.StringIO(csv_content), sep=delimiter, quotechar=quotechar, dtype=str, keep_default_na=False)
    
    if "confidence" in df.columns and "legal_form" in df.columns:
        df.loc[df["confidence"].str.lower() == "unknown", "legal_form"] = ""
        
        # Also apply the sanitization step here for consistency with the main pipeline
        sanitized_count = 0
        def _apply_sanitize(val):
            nonlocal sanitized_count
            if pd.isna(val) or not str(val).strip():
                return val
            orig = str(val)
            if orig.lower() != "unknown":
                sanitized = sanitize_legal_form(orig)
                if sanitized and sanitized != orig:
                    sanitized_count += 1
                    return sanitized
            return orig
            
        df["legal_form"] = df["legal_form"].apply(_apply_sanitize)
        if sanitized_count:
            logger.info(f"Sanitize pass: normalized {sanitized_count} legal forms")

    logger.info(f"Writing updated CSV: {output_path}")
    df.to_csv(output_path, index=False, encoding="utf-8-sig", sep=";")

    
    pipeline = ClassificationPipeline(offline=True)
    results = []
    
    def _safe_get(row_series, col_name, default_val=""):
        return row_series.get(col_name) if col_name in row_series and pd.notna(row_series.get(col_name)) else default_val
        
    for _, row in df.iterrows():
        results.append(ClassificationResult(
            organisation_name=str(_safe_get(row, "organisation_name", "")),
            legal_form=str(_safe_get(row, "legal_form", "")),
            confidence=str(_safe_get(row, "confidence", "unknown")),
            source=str(_safe_get(row, "source", "")),
            link_to_src=str(_safe_get(row, "link_to_src", ""))
        ))
        
    pipeline._log_statistics(results)
    stats = pipeline._compute_statistics(results)
    
    try:
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
