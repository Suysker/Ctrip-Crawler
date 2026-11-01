#!/usr/bin/env python3
"""Ctrip flight collector — standalone source distribution.

Python 3.12+. Install the exact supported runtime (no repository required):
  python -m pip install camoufox==0.5.8 playwright==1.60.0 fpgen==1.3.0 filelock==4.2.6 psutil==7.2.2 tzdata==2026.6
  python -m camoufox fetch official/152.0.4-beta.29
  python -c "from camoufox.fpgen_model import ensure_fpgen_model; ensure_fpgen_model()"
  python ctrip_flights_scraper.py --help

Authentication files contain private session data. Do not publish the data directory.
Generated from source_sections; all application code is contained in this file.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import csv
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import logging
import logging.handlers
import math
import multiprocessing
import os
import platform
import queue
import random
import re
import shutil
import signal
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse, urlencode
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1
VERSION = '4.0.0'
BROWSER_BUILD = 'official/152.0.4-beta.29'
RUNTIME_REQUIREMENTS = {
    'camoufox': '0.5.8', 'playwright': '1.60.0', 'fpgen': '1.3.0',
    'filelock': '4.2.6', 'psutil': '7.2.2', 'tzdata': '2026.6',
}


class CrawlerError(Exception):
    """A boundary failure with an actionable, stable category."""

    def __init__(self, code, message, *, retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_utc(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise CrawlerError('state_invalid', '时间戳必须包含时区')
    return value.astimezone(timezone.utc)


def stable_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def safe_message(error):
    """Do not emit URLs, request payloads, or credentials from third-party errors."""
    if isinstance(error, CrawlerError):
        return str(error)
    return type(error).__name__
