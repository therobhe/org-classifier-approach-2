import diskcache
from pathlib import Path
from typing import Optional
from .models import ClassificationResult
import json


class ClassificationCache:
    """Disk-based cache for classification results."""
    
    def __init__(self, cache_dir: str = ".cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.cache = diskcache.Cache(str(self.cache_dir))
    
    def get(self, organisation_name: str) -> Optional[ClassificationResult]:
        """Retrieve cached classification result."""
        key = self._make_key(organisation_name)
        data = self.cache.get(key)
        
        if data:
            return ClassificationResult(**json.loads(data))
        return None
    
    def set(self, result: ClassificationResult) -> None:
        """Store classification result in cache."""
        key = self._make_key(result.organisation_name)
        self.cache.set(key, result.model_dump_json())
    
    def _make_key(self, organisation_name: str) -> str:
        """Generate cache key from organisation name."""
        return f"org:{organisation_name.strip().lower()}"
    
    def clear(self) -> None:
        """Clear all cached results."""
        self.cache.clear()
    
    def close(self) -> None:
        """Close the cache."""
        self.cache.close()
