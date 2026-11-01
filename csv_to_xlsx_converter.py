"""Merge flight CSVs into one text-preserving XLSX workbook per route.

Optional dependency: python -m pip install openpyxl==3.1.5
Input layout: <input>/<departure-date>/<observed-date>/<route>.csv
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import date
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile


# Edit these defaults, or pass command-line options. Dates are YYYY-MM-DD or None.
BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / 'data'
OUTPUT_DIR = BASE_DIR / 'xlsx_output'
DEPARTURE_START = None
DEPARTURE_END = None
OBSERVED_DATE = None

MAX_ROWS = 1_048_576  # Includes the header row.
MAX_COLUMNS = 16_384
MAX_CELL_CHARACTERS = 32_767
# XML 1.0 excludes these control characters; do not silently remove them.
INVALID_XML = re.compile(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]')


class ConversionError(ValueError):
    """The input cannot be exported without changing or losing data."""


def _check_text(value, path, row_number):
    if len(value) > MAX_CELL_CHARACTERS:
        raise ConversionError(f'{path}: 第 {row_number} 行有单元格超过 Excel 的 32767 字符上限')
    if INVALID_XML.search(value):
        raise ConversionError(f'{path}: 第 {row_number} 行含 XLSX 无法保存的控制字符')


@contextmanager
def _csv_reader(path):
    try:
        with path.open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.reader(handle, strict=True)
            headers = next(reader, None)
            if not headers:
                raise ConversionError(f'{path}: CSV 缺少表头')
            if len(headers) != len(set(headers)):
                raise ConversionError(f'{path}: CSV 表头重复，无法无损合并')
            for value in headers:
                _check_text(value, path, 1)
            yield headers, reader
    except (OSError, UnicodeError, csv.Error) as error:
        raise ConversionError(f'{path}: 无法读取 UTF-8 CSV：{error}') from error


def _checked_rows(path, headers, reader):
    for row_number, row in enumerate(reader, 2):
        if not row:
            continue
        if len(row) != len(headers):
            raise ConversionError(f'{path}: 第 {row_number} 行字段数与表头不一致')
        for value in row:
            _check_text(value, path, row_number)
        yield row


def _inspect_sources(paths):
    columns, seen, sources = [], set(), []
    row_count = 0
    for path in paths:
        with _csv_reader(path) as (headers, reader):
            for header in headers:
                if header not in seen:
                    columns.append(header)
                    seen.add(header)
            if len(columns) > MAX_COLUMNS:
                raise ConversionError(f'合并列数超过 Excel 的 {MAX_COLUMNS} 列上限')
            for _ in _checked_rows(path, headers, reader):
                row_count += 1
                if row_count + 1 > MAX_ROWS:
                    raise ConversionError(f'合并行数超过 Excel 的 {MAX_ROWS} 行上限（含表头）')
            sources.append((path, headers))
    return columns, sources, row_count


def merge_csv_files(csv_files, output_xlsx, *, overwrite=False):
    """Preserve source column names and values with bounded row memory.

    Values remain text. Missing columns and empty CSV fields become blank cells;
    dates and prices are never inferred, renamed or converted to numbers.
    """
    paths, output = [Path(path) for path in csv_files], Path(output_xlsx)
    if not paths:
        raise ConversionError('没有可合并的 CSV 文件')
    if output.exists() and not overwrite:
        raise ConversionError(f'输出文件已存在：{output}；确认替换时使用 --overwrite')
    try:
        from openpyxl import Workbook
        from openpyxl.cell import WriteOnlyCell
    except ImportError as error:
        raise ConversionError('请先安装转换依赖：python -m pip install openpyxl==3.1.5') from error

    # Preflight streams all rows before a destination can be replaced.
    columns, sources, expected_rows = _inspect_sources(paths)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f'.{output.stem}.', suffix='.xlsx', dir=output.parent)
    os.close(descriptor)
    temporary = Path(name)
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet('Flights')
    sheet.freeze_panes = 'A2'
    positions = {column: index for index, column in enumerate(columns)}

    def cells(values):
        for value in values:
            cell = WriteOnlyCell(sheet, value=value)
            cell.data_type = 's'  # Includes formulas and Excel error-like strings.
            cell.number_format = '@'
            yield cell

    try:
        sheet.append(list(cells(columns)))
        written = 0
        for path, expected_headers in sources:
            with _csv_reader(path) as (headers, reader):
                if headers != expected_headers:
                    raise ConversionError(f'{path}: 转换期间 CSV 表头发生变化')
                indices = [positions[header] for header in headers]
                for row in _checked_rows(path, headers, reader):
                    written += 1
                    if written + 1 > MAX_ROWS:
                        raise ConversionError(f'合并行数超过 Excel 的 {MAX_ROWS} 行上限（含表头）')
                    ordered = [''] * len(columns)
                    for index, value in zip(indices, row):
                        ordered[index] = value
                    sheet.append(list(cells(ordered)))
        if written != expected_rows:
            raise ConversionError('转换期间 CSV 行数发生变化，请使用稳定的输入文件重试')
        workbook.save(temporary)
        if overwrite:
            os.replace(temporary, output)
        else:
            # Exclusive creation also protects an output created after preflight.
            created = False
            try:
                with output.open('xb') as destination:
                    created = True
                    with temporary.open('rb') as source:
                        shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
            except BaseException:
                if created:
                    output.unlink(missing_ok=True)
                raise
        return {'output': str(output), 'files': len(paths), 'rows': written, 'columns': len(columns)}
    except OSError as error:
        raise ConversionError(f'无法写入 {output}：{error}') from error
    finally:
        workbook.close()
        temporary.unlink(missing_ok=True)


def _filter_date(value):
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ConversionError(f'日期必须为 YYYY-MM-DD：{value}') from error


def convert_directory(input_dir=INPUT_DIR, output_dir=OUTPUT_DIR, *, departure_start=None,
                      departure_end=None, observed_date=None, overwrite=False):
    """Group date/date/route.csv files by their unchanged route filename."""
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    start, end, observed = map(_filter_date, (departure_start, departure_end, observed_date))
    if start and end and end < start:
        raise ConversionError('出发日期结束值不能早于起始值')
    if not input_dir.is_dir():
        raise ConversionError(f'输入目录不存在：{input_dir}')
    routes = {}
    for path in sorted(input_dir.glob('*/*/*')):
        if not path.is_file() or path.suffix.lower() != '.csv':
            continue
        try:
            departure = date.fromisoformat(path.parent.parent.name)
            observation = date.fromisoformat(path.parent.name)
        except ValueError:
            continue
        if ((start and departure < start) or (end and departure > end)
                or (observed and observation != observed)):
            continue
        routes.setdefault(path.stem, []).append(path)
    if not routes:
        raise ConversionError('没有符合目录布局和日期筛选条件的 CSV 文件')
    if not overwrite:
        conflicts = [output_dir / (route + '.xlsx') for route in routes
                     if (output_dir / (route + '.xlsx')).exists()]
        if conflicts:
            raise ConversionError(f'输出文件已存在：{conflicts[0]}；确认替换时使用 --overwrite')
    reports = []
    for route, paths in routes.items():
        report = merge_csv_files(paths, output_dir / (route + '.xlsx'), overwrite=overwrite)
        reports.append(dict(report, route=route))
    return reports


def main(argv=None):
    parser = argparse.ArgumentParser(description='按航线合并 CSV，保留全部列及文本值，生成 XLSX')
    parser.add_argument('--input', type=Path, default=INPUT_DIR, help='输入根目录，默认脚本旁 data/')
    parser.add_argument('--output', type=Path, default=OUTPUT_DIR, help='输出目录，默认脚本旁 xlsx_output/')
    parser.add_argument('--departure-start', default=DEPARTURE_START, metavar='YYYY-MM-DD')
    parser.add_argument('--departure-end', default=DEPARTURE_END, metavar='YYYY-MM-DD')
    parser.add_argument('--observed-date', default=OBSERVED_DATE, metavar='YYYY-MM-DD')
    parser.add_argument('--overwrite', action='store_true', help='明确替换已有同名 XLSX')
    args = parser.parse_args(argv)
    try:
        reports = convert_directory(args.input, args.output, departure_start=args.departure_start,
            departure_end=args.departure_end, observed_date=args.observed_date, overwrite=args.overwrite)
    except (ConversionError, OSError) as error:
        print(f'转换失败：{error}', file=sys.stderr)
        return 1
    for report in reports:
        print(f"{report['route']}：{report['files']} 个 CSV，{report['rows']} 行，"
              f"{report['columns']} 列 -> {report['output']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
