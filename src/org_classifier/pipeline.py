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
        offline: bool = False,
        with_heuristic: bool = False
    ):
        self.cache = ClassificationCache(cache_dir)
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)
        self.offline = offline
        self.with_heuristic = with_heuristic
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
    
    async def classify_organisation_web(self, org_name: str) -> ClassificationResult:
        """
        Expensive classification using web search / LLM API.
        Only called for organisations that couldn't be classified by fast methods.
        
        Returns a classification result (may be 'unknown' if web search also fails).
        """
        # Stage 2: Web search + ChatGPT (skip if offline)
        if not self.offline:
            # TODO: implement the ChatGPT/LLM classification here, using self.http_client for any web requests.
            # This should:
            # 1. Call an LLM API with the organisation name
            # 2. Parse the JSON response { "legal_form": "...", "src": "..." }
            # 3. Return a ClassificationResult with link_to_src populated
            pass
        
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
        
        # Pass 2: Web search for unknowns only
        if unknowns and not self.offline:
            logger.info(f"Pass 2: Web search for {len(unknowns)} unknowns...")
            pbar = tqdm(total=len(unknowns), desc="Web classification") if show_progress else None
            
            async def _classify_web_with_progress(name: str) -> ClassificationResult:
                result = await self.classify_organisation_web(name)
                if pbar:
                    pbar.update(1)
                return result
            
            tasks = [_classify_web_with_progress(name) for name in unknowns]
            web_results = await asyncio.gather(*tasks)
            
            if pbar:
                pbar.close()
            
            # Add web results to classified_results
            for org_name, result in zip(unknowns, web_results):
                classified_results[org_name] = result
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
