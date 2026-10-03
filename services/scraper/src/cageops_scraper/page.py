from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Page:
    url: str
    status: int
    html: str
    fetched_at: datetime
    from_cache: bool
