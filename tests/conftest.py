"""Shared pytest configuration.

The mock emitters sleep 10-110 ms between log lines so live runs produce
realistically interleaved timestamps. That real time is pointless in tests and
makes the suite crawl, so we disable it for the whole test session via the
``MOCK_EMIT_NO_SLEEP`` flag that ``pipeline/services/blocklib.py`` honors. Setting
it here (before the service modules are imported) means no test has to remember to.
"""
import os

os.environ.setdefault("MOCK_EMIT_NO_SLEEP", "1")
