"""
conftest.py — pytest path setup for the whole project.
Adds both the project root and services/router_parser so that
bare imports like `from parsers_base import ...` work in tests.
"""
import sys
import os

ROOT = os.path.abspath(os.path.dirname(__file__))
ROUTER_PARSER = os.path.join(ROOT, "services", "router_parser")

for p in (ROOT, ROUTER_PARSER):
    if p not in sys.path:
        sys.path.insert(0, p)
