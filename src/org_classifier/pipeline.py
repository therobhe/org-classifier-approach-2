import asyncio
import httpx
import pandas as pd
from pathlib import Path
from typing import List, Optional, Tuple
import logging
from tqdm import tqdm
import csv
from pandas.errors import ParserError

from .models import ClassificationResult
from .cache import ClassificationCache
from .classifiers.name_extractor import classify_by_name
from .classifiers.web_search import find_impressum_url
from .classifiers.impressum import classify_by_impressum
from .classifiers.heuristic import classify_by_heuristic, classify_as_unknown
from .party_rules import classify_party
from .utils import normalize_org_name

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
        offline: bool = False
    ):
        self.cache = ClassificationCache(cache_dir)
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)
        self.offline = offline
        self.http_client: Optional[httpx.AsyncClient] = None
    
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
    
    async def classify_organisation(self, org_name: str) -> ClassificationResult:
        """
        Classify a single organisation through the cascade:
        1. Check cache
        2. Name extraction (regex)
        3. Web search + Impressum (if not offline)
        4. Heuristics
        5. Unknown
        """
        # Check cache first
        cached = self.cache.get(org_name)
        if cached:
            logger.debug(f"Cache hit for: {org_name}")
            return cached
        
        # Stage 1: Name extraction
        result = classify_by_name(org_name)
        if result:
            self.cache.set(result)
            return result
        
        # Stage 2: Web search + Impressum (skip if offline)
        if not self.offline and self.http_client:
            async with self.semaphore:
                try:
                    impressum_url = await find_impressum_url(org_name)
                    if impressum_url:
                        result = await classify_by_impressum(
                            org_name,
                            impressum_url,
                            self.http_client
                        )
                        if result:
                            self.cache.set(result)
                            return result
                except Exception as e:
                    logger.warning(f"Web search failed for '{org_name}': {e}")
        
        # Stage 3: Heuristics
        # First, check party regex rules (high-confidence, local-only)
        try:
            party_match = classify_party(org_name)
        except Exception:
            party_match = None
        if party_match:
            result = ClassificationResult(
                organisation_name=org_name,
                legal_form=party_match.get("legal_form"),
                confidence=party_match.get("confidence", "high"),
                source=party_match.get("source", "party_regex_rule"),
            )
            self.cache.set(result)
            return result
        result = classify_by_heuristic(org_name)
        if result:
            self.cache.set(result)
            return result
        
        # Stage 4: Unknown
        result = classify_as_unknown(org_name)
        self.cache.set(result)
        return result
    
    async def process_batch(
        self,
        org_names: List[str],
        show_progress: bool = True
    ) -> List[ClassificationResult]:
        """Process a batch of organisations concurrently.

        Uses asyncio.gather to preserve input order (critical for
        correct index-based merging with the source DataFrame).
        """
        pbar = tqdm(total=len(org_names), desc="Classifying organisations") if show_progress else None

        async def _classify_with_progress(name: str) -> ClassificationResult:
            result = await self.classify_organisation(name)
            if pbar:
                pbar.update(1)
            return result

        tasks = [_classify_with_progress(name) for name in org_names]
        results = await asyncio.gather(*tasks)

        if pbar:
            pbar.close()

        return list(results)
    
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

        # Build a clean output with exactly 4 columns:
        #   organisation_name | legal_form | confidence | source
        # This avoids carrying over junk columns from semicolon-heavy inputs.
        output_df = pd.DataFrame(
            {
                "organisation_name": org_names,
                "legal_form": [r.legal_form for r in results],
                "confidence": [r.confidence for r in results],
                "source": [r.source for r in results],
            }
        )

        # Write output CSV (semicolon-separated + UTF-8 BOM for German Excel)
        logger.info(f"Writing output CSV: {output_path}")
        output_df.to_csv(output_path, index=False, encoding="utf-8-sig", sep=";")
        
        # Log statistics
        self._log_statistics(results)
    
    def _log_statistics(self, results: List[ClassificationResult]) -> None:
        """Log classification statistics."""
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
        
        logger.info(f"\nClassification Statistics:")
        logger.info(f"Total organisations: {total}")
        logger.info(f"\nBy confidence:")
        for conf, count in sorted(by_confidence.items()):
            pct = (count / total) * 100
            logger.info(f"  {conf}: {count} ({pct:.1f}%)")
        
        logger.info(f"\nBy source:")
        for src, count in sorted(by_source.items()):
            pct = (count / total) * 100
            logger.info(f"  {src}: {count} ({pct:.1f}%)")

        # Terminal-friendly Rechtsform distribution
        logger.info(f"\nBy legal form:")
        forms_sorted = sorted(by_legal_form.items(), key=lambda kv: (-kv[1], str(kv[0])))
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
