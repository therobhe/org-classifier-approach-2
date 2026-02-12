import httpx
from bs4 import BeautifulSoup
from typing import Optional
from tenacity import retry, stop_after_attempt, wait_exponential
import logging
from ..models import ClassificationResult
from ..constants import LEGAL_FORMS

logger = logging.getLogger(__name__)


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=5))
async def fetch_page(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Fetch HTML content from URL with retry logic."""
    try:
        response = await client.get(
            url,
            timeout=10.0,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
        )
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return None


def extract_legal_form_from_html(html: str) -> Optional[tuple[str, str]]:
    """
    Extract legal form from HTML content using regex patterns.
    Returns (legal_form, confidence) tuple or None.
    """
    soup = BeautifulSoup(html, 'lxml')
    
    # Extract text content
    text = soup.get_text(separator=' ', strip=True)
    
    # Look for legal forms in text
    for legal_form, pattern in LEGAL_FORMS:
        matches = pattern.findall(text)
        if matches:
            # High confidence if found multiple times or in title/headings
            title_text = soup.title.string if soup.title else ""
            headings = ' '.join([h.get_text() for h in soup.find_all(['h1', 'h2', 'h3'])])
            
            if pattern.search(title_text) or pattern.search(headings) or len(matches) >= 2:
                return legal_form, "high"
            else:
                return legal_form, "medium"
    
    return None


async def classify_by_impressum(
    organisation_name: str,
    url: str,
    client: httpx.AsyncClient
) -> Optional[ClassificationResult]:
    """
    Fetch and parse Impressum page to extract legal form.
    """
    html = await fetch_page(url, client)
    
    if not html:
        return None
    
    result = extract_legal_form_from_html(html)
    
    if result:
        legal_form, confidence = result
        return ClassificationResult(
            organisation_name=organisation_name,
            legal_form=legal_form,
            confidence=confidence,
            source=url
        )
    
    return None
