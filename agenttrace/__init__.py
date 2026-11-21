"""AgentTrace — fleet-wide, tamper-evident trace & audit store.

One trace ID follows a unit of work across agents and repos; every event
lands in one hash-chained, append-only JSONL store that a verifier can
check end-to-end. See README.md for the boundary table (Ledger = cost,
Gate = enforcement, AgentTrace = the fleet record).
"""

from .schema import SCHEMA_VERSION

__version__ = "0.1.0"
__all__ = ["SCHEMA_VERSION", "__version__"]
