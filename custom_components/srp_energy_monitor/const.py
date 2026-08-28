"""Constants for the SRP Energy Monitor integration."""

from __future__ import annotations

import logging
from datetime import timedelta

DOMAIN = "srp_energy_monitor"
LOGGER = logging.getLogger(__package__)

CONF_ACCOUNT_ID = "account_id"

MANUFACTURER = "srpnet.com"
MODEL = "My Account JSON API"
CONFIGURATION_URL = "https://myaccount.srpnet.com/power/myaccount/usage"

# SRP settles the previous day overnight and publishes nothing new in between,
# so polling harder buys nothing. Four hours matches the built-in integration
# and leaves room for a late settle without hammering someone else's site.
UPDATE_INTERVAL = timedelta(hours=4)

# How far back a first-time backfill reaches. Measured on the account this was
# built against: hourly energy from 2024-07-29, and both generation and
# half-hourly demand from 2025-05-04, when the array came online and the
# account moved to a demand plan. Asking for more than exists is harmless --
# the windows simply come back empty -- so this is generous on purpose.
BACKFILL_DAYS = 760

# External statistic ids. The recorder validates these against
# ^(?!.+__)(?!_)[\da-z_]+(?<!_):(?!_)[\da-z_]+(?<!_)$ -- no double underscore
# anywhere, and the prefix must equal the metadata "source".
STAT_CONSUMPTION = "energy_consumption"
STAT_GENERATION = "energy_generation"
STAT_NET = "energy_net"
STAT_DEMAND = "demand_kw"
