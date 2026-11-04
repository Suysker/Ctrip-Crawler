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
VERSION = '4.1.0'
BROWSER_BUILD = 'official/152.0.4-beta.29'
RUNTIME_REQUIREMENTS = {
    'camoufox': '0.5.8', 'playwright': '1.60.0', 'fpgen': '1.3.0',
    'filelock': '4.2.6', 'psutil': '7.2.2', 'tzdata': '2026.6',
}
DIAGNOSTIC_MAX_BYTES = 8192


class CrawlerError(Exception):
    """A boundary failure with an actionable, stable category."""

    def __init__(self, code, message, *, retry_after=None, diagnostic=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.diagnostic = dict(diagnostic or {})


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


def _diagnostic_secrets(config, command):
    values = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('username', 'password', 'token', 'access_token', 'refresh_token', 'cookie', 'authorization'):
                    if isinstance(item, str) and item:
                        values.add(item)
                elif isinstance(item, (dict, list, tuple)):
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(config)
    visit(command)
    return sorted(values, key=len, reverse=True)


def _diagnostic_text(value, secrets, limit=1024):
    text = str(value)

    def origin(match):
        try:
            parsed = urlparse(match.group())
            host = parsed.hostname
            if not host:
                return '[url omitted]'
            port = ':' + str(parsed.port) if parsed.port is not None else ''
            return '[url ' + parsed.scheme + '://' + host + port + ']'
        except ValueError:
            return '[url omitted]'

    text = re.sub(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s<>"\']+', origin, text)
    for secret in secrets:
        text = text.replace(secret, '[redacted]')
        escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
        if escaped != secret:
            text = text.replace(escaped, '[redacted]')
    text = re.sub(r'(?im)\b(?:authorization|cookie|set-cookie)\s*:[^\r\n]*', '[private header omitted]', text)
    text = re.sub(r'(?i)\b(?:password|passwd|pwd|username|account|token|secret)\s*[:=]\s*(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s,;]+)',
                  '[private value omitted]', text)
    text = re.sub(r'(?is)\b(?:payload|response|body)\s*[:=]\s*[\[{].*', '[structured data omitted]', text)
    text = re.sub(r'(?s)\{.*(?:\}|$)', '[structured data omitted]', text)
    text = re.sub(r'(["\'])(?:[A-Za-z]:[\\/]|\\\\|/).*?\1', '[path omitted]', text)
    text = re.sub(r'(?<!\w)[A-Za-z]:[\\/][^\s"\'<>]*|(?<![\w:/])/(?:[^\s"\'<>]+)|\\\\[^\s"\'<>]+', '[path omitted]', text)
    return ' '.join(text.split())[:limit]


def safe_message(error, config=None, command=None):
    """Keep the useful failure text while removing private inputs and paths."""
    return _diagnostic_text(error, _diagnostic_secrets(config, command))


def firefox_error_code(error):
    """Read Firefox's error symbol, never infer it from prose or a URL."""
    first_line = str(error).split('\n', 1)[0]
    match = re.match(r'^\s*(?:[A-Za-z_][A-Za-z0-9_.]*:\s*)?(NS_ERROR_[A-Z0-9_]{1,64})(?:\s|$)', first_line)
    return match.group(1) if match else None


def make_diagnostic(error, *, stage=None, config=None, command=None):
    """One bounded diagnostic schema for local errors and worker failures."""
    secrets = _diagnostic_secrets(config, command)
    supplied = getattr(error, 'diagnostic', {}) or {}
    name = supplied.get('exception_type', type(error).__name__)
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', name):
        name = type(error).__name__
    result = {'exception_type': name,
              'message': _diagnostic_text(supplied.get('message', str(error)), secrets),
              'stage': _diagnostic_text(supplied.get('stage') or stage or 'unknown', secrets, 80),
              'frames': []}
    frames = supplied.get('frames')
    if not isinstance(frames, list):
        frames = [{'file': frame.filename, 'line': frame.lineno, 'function': frame.name}
                  for frame in traceback.extract_tb(error.__traceback__, limit=8)]
    for frame in frames[-8:]:
        if isinstance(frame, dict) and type(frame.get('line')) is int:
            filename = str(frame.get('file', '')).replace('\\', '/').rsplit('/', 1)[-1]
            result['frames'].append({'file': _diagnostic_text(filename, secrets, 120),
                                     'line': frame['line'],
                                     'function': _diagnostic_text(frame.get('function', ''), secrets, 120)})
    protocol = supplied.get('protocol')
    if isinstance(protocol, dict):
        summary = {}
        for key in ('http_status', 'business_status', 'list_count'):
            value = protocol.get(key)
            if value is None or (type(value) is int and abs(value) < 10 ** 12):
                if key in protocol:
                    summary[key] = value
        for key in ('finished', 'list_present', 'known_snapshot', 'request_matches', 'transaction_present',
                    'transaction_matches', 'search_id_present', 'search_id_matches'):
            if key in protocol and (protocol[key] is None or type(protocol[key]) is bool):
                summary[key] = protocol[key]
        for key in ('finished_type', 'list_type'):
            if protocol.get(key) in ('missing', 'NoneType', 'bool', 'dict', 'list', 'int', 'str', 'float'):
                summary[key] = protocol[key]
        result['protocol'] = summary
    network = supplied.get('network')
    network = network if isinstance(network, dict) else {}
    summary = {}
    symbol = network.get('browser_error_code') or firefox_error_code(error)
    if isinstance(symbol, str) and firefox_error_code(symbol) == symbol:
        summary['browser_error_code'] = _diagnostic_text(symbol, secrets, 80)
    if network.get('response_kind') in ('navigation', 'search', 'comfort', 'resolve', 'auth'):
        summary['response_kind'] = network['response_kind']
    if network.get('method') in ('GET', 'POST'):
        summary['method'] = network['method']
    for key in ('http_status', 'failed_request_count', 'search_responses_received'):
        value = network.get(key)
        if type(value) is int and 0 <= value < 10 ** 12:
            summary[key] = value
        elif key == 'http_status' and key in network and value is None:
            summary[key] = None
    if type(network.get('request_failed')) is bool:
        summary['request_failed'] = network['request_failed']
    if summary:
        result['network'] = summary
    causes = []
    if isinstance(supplied.get('causes'), list):
        for cause in supplied['causes'][:3]:
            if (isinstance(cause, dict) and isinstance(cause.get('exception_type'), str)
                    and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', cause['exception_type'])):
                causes.append({'exception_type': cause['exception_type'],
                               'message': _diagnostic_text(cause.get('message', ''), secrets, 256)})
    else:
        cause = error.__cause__
        while cause is not None and len(causes) < 3:
            causes.append({'exception_type': type(cause).__name__, 'message': _diagnostic_text(cause, secrets, 256)})
            cause = cause.__cause__
    if causes:
        result['causes'] = causes
    while len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > DIAGNOSTIC_MAX_BYTES:
        result['truncated'] = True
        if result['frames']:
            result['frames'].pop(0)
        elif result.get('causes'):
            result['causes'].pop()
        else:
            result['message'] = result['message'][:len(result['message']) // 2]
    return result
