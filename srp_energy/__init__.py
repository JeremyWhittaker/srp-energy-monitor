"""SRP Energy Monitor -- collect, store and serve Salt River Project energy data.

Not affiliated with or endorsed by Salt River Project.

Everything exported here is browser-free: parsing and storage work on CSVs you
already have, so this package can be used as a library without installing
Playwright. The scraper lives in ``srp_energy.client`` and is imported lazily.
"""

from .models import (
    BillProjection,
    DailyReading,
    DemandObservation,
    IntervalReading,
    Series,
)
from .parser import (
    ParseError,
    parse_csv,
    parse_file,
    parse_hourly_csv,
    parse_hourly_file,
)
from .storage import Store

__version__ = "1.1.0"

__all__ = [
    # models
    "BillProjection",
    "DailyReading",
    "DemandObservation",
    "IntervalReading",
    "Series",
    # parsing
    "ParseError",
    "parse_csv",
    "parse_file",
    "parse_hourly_csv",
    "parse_hourly_file",
    # storage
    "Store",
    "__version__",
]
