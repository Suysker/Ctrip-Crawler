"""Lossless, standalone conversion of legacy and current flight CSVs."""
import csv
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import openpyxl
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'csv_to_xlsx_converter.py'


@pytest.fixture
def converter():
    spec = importlib.util.spec_from_file_location('converter_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_csv(path, headers, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def workbook_rows(path):
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=False)
    try:
        return list(workbook.active.iter_rows(values_only=True))
    finally:
        workbook.close()


def test_import_does_not_convert_or_create_output(tmp_path):
    code = ('import importlib.util; '
            f'spec=importlib.util.spec_from_file_location("converter",{str(SCRIPT)!r}); '
            'module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)')
    result = subprocess.run([sys.executable, '-c', code], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not result.stdout
    assert not list(tmp_path.iterdir())


def test_real_legacy_and_new_columns_are_unioned_without_changing_values(converter, tmp_path):
    source = sorted((ROOT / 'data_examples').glob('*/*/上海-东京.csv'))[0]
    original_path = tmp_path / 'input' / source.relative_to(ROOT / 'data_examples')
    original_path.parent.mkdir(parents=True)
    shutil.copyfile(source, original_path)
    with source.open(encoding='utf-8-sig', newline='') as handle:
        original = list(csv.reader(handle))
    headers = ['批次标识', '任务标识', '行程标识', '出发日期', 'economy_origin',
               '价格状态', '价格不完整原因', '舒适度状态', '结构字段', '公式文本', '错误文本', '空值']
    values = ['000123', '000009', '000008', '1999-01-01', '00700.00',
              'partial', 'tax_missing;total_missing', 'unavailable',
              '{"items":[1,2],"note":"保持文本"}', '=SUM(1,2)', '#N/A', '']
    write_csv(tmp_path / 'input/2024-04-02/2024-03-31/上海-东京.csv', headers, [values])
    reports = converter.convert_directory(tmp_path / 'input', tmp_path / 'output')
    assert len(reports) == 1
    rows = workbook_rows(tmp_path / 'output/上海-东京.xlsx')
    union = list(rows[0])
    assert union == list(dict.fromkeys(original[0] + headers))
    assert len(rows) == len(original) + 1
    assert '票价' not in union
    for expected, actual in zip(original[1:], rows[1:-1]):
        assert [actual[union.index(column)] or '' for column in original[0]] == expected
    final = dict(zip(union, rows[-1]))
    assert [final[column] or '' for column in headers] == values
    workbook = openpyxl.load_workbook(tmp_path / 'output/上海-东京.xlsx', read_only=True)
    try:
        sheet = workbook.active
        assert sheet.cell(len(rows), union.index('公式文本') + 1).data_type == 's'
        assert sheet.cell(len(rows), union.index('错误文本') + 1).data_type == 's'
    finally:
        workbook.close()


def test_date_filters_and_original_route_names_control_grouping(converter, tmp_path):
    for departure, observed, route in [('2027-01-01', '2026-12-01', '甲-乙'),
                                        ('2027-01-02', '2026-12-02', '甲-乙'),
                                        ('2027-01-02', '2026-12-01', '乙-甲'),
                                        ('2027-01-03', '2026-12-01', '甲-乙')]:
        write_csv(tmp_path / 'input' / departure / observed / (route + '.csv'),
                  ['航班号'], [[departure]])
    reports = converter.convert_directory(tmp_path / 'input', tmp_path / 'output',
        departure_start='2027-01-02', departure_end='2027-01-02', observed_date='2026-12-01')
    assert len(reports) == 1
    assert sorted(path.name for path in (tmp_path / 'output').glob('*.xlsx')) == ['乙-甲.xlsx']
    assert workbook_rows(tmp_path / 'output/乙-甲.xlsx') == [('航班号',), ('2027-01-02',)]


def test_existing_output_requires_explicit_overwrite_and_errors_preserve_it(converter, tmp_path):
    source = write_csv(tmp_path / '2027-01-01/2026-12-01/甲-乙.csv', ['航班号'], [['0001']])
    output = tmp_path / 'route.xlsx'
    output.write_bytes(b'keep existing workbook')
    with pytest.raises(converter.ConversionError):
        converter.merge_csv_files([source], output)
    assert output.read_bytes() == b'keep existing workbook'
    write_csv(source, ['航班号'], [['x' * 32768]])
    with pytest.raises(converter.ConversionError):
        converter.merge_csv_files([source], output, overwrite=True)
    assert output.read_bytes() == b'keep existing workbook'
    write_csv(source, ['航班号'], [['0001']])
    converter.merge_csv_files([source], output, overwrite=True)
    assert workbook_rows(output) == [('航班号',), ('0001',)]


@pytest.mark.parametrize('limit', ['rows', 'columns', 'cell'])
def test_excel_limits_reject_instead_of_truncating(converter, tmp_path, monkeypatch, limit):
    headers, rows = ['text'], [['one']]
    if limit == 'rows':
        monkeypatch.setattr(converter, 'MAX_ROWS', 3)
        rows = [['one'], ['two'], ['three']]
    elif limit == 'columns':
        monkeypatch.setattr(converter, 'MAX_COLUMNS', 2)
        headers, rows = ['one', 'two', 'three'], []
    else:
        rows = [['x' * 32768]]
    source = write_csv(tmp_path / 'source.csv', headers, rows)
    output = tmp_path / 'route.xlsx'
    with pytest.raises(converter.ConversionError):
        converter.merge_csv_files([source], output)
    assert not output.exists()


@pytest.mark.parametrize('headers,rows', [(['same', 'same'], [['one', 'two']]),
                                         (['one', 'two'], [['missing']])])
def test_ambiguous_or_ragged_csv_does_not_silently_lose_fields(converter, tmp_path, headers, rows):
    source = write_csv(tmp_path / 'source.csv', headers, rows)
    with pytest.raises(converter.ConversionError):
        converter.merge_csv_files([source], tmp_path / 'route.xlsx')
    assert not (tmp_path / 'route.xlsx').exists()


def test_header_only_csv_is_a_valid_empty_workbook(converter, tmp_path):
    source = write_csv(tmp_path / 'source.csv', ['航班号', '价格状态'], [])
    converter.merge_csv_files([source], tmp_path / 'route.xlsx')
    assert workbook_rows(tmp_path / 'route.xlsx') == [('航班号', '价格状态')]
