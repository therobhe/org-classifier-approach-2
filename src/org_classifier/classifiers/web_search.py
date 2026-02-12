import asyncio
from typing import Optional, List
from tenacity import retry, stop_after_attempt, wait_exponential
import logging

logger = logging.getLogger(__name__)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def search_google(query: str, num_results: int = 3) -> List[str]:
    """
    Search Google for a query and return URLs.
    Uses googlesearch-python which is rate-limited.
    """
    try:
        # Import here to avoid issues if not installed
        from googlesearch import search
        
        # Run in executor since googlesearch is synchronous
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(
            None,
            lambda: list(search(query, num_results=num_results, lang="de"))
        )
        return results
    except Exception as e:
        logger.warning(f"Google search failed for '{query}': {e}")
        return []


async def find_impressum_url(organisation_name: str) -> Optional[str]:
    """
    Search for organisation's Impressum page and return the URL.
    """
    query = f'"{organisation_name}" Impressum'
    urls = await search_google(query, num_results=3)
    
    if urls:
        # Return first result - most likely to be official Impressum
        return urls[0]
    
    return None
