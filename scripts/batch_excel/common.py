"""用途：两个 Excel 导出脚本共用的 JSON 校验、对象遍历、排版和文件写入模块。

本文件不是独立命令行入口，直接运行不会生成 Excel，也不需要在这里配置批次路径。
请使用同目录 build_workbook.py 或 build_hierarchy.py，在入口脚本顶部配置
INPUT_JSON / OUTPUT_DIR，或通过输入 JSON 参数和 --output-dir 指定路径。
复制导出脚本到其他位置时，本文件和 temporal.py 必须一起放到同一目录；依赖 openpyxl。

主要职责：
1. 校验英文层级结构和属性类型，拒绝重复 JSON 键、旧表单次序字段及非法文本。
2. 仅在同一表单、同一类别内合并完全相同的对象，保留不同角色和不同表单实例。
3. 保护编号前导零和超长整数，将公式形式的文本作为普通文本写入。
4. 在输出目录写入临时工作簿，回读核对所有单元格后才发布最终文件。
5. 已有输出默认受保护，只有入口传入 --overwrite 时才允许替换。

需要扩展通用导出行为时再修改本模块；仅增加 JSON 属性无需修改代码。
验证命令（在 PDFToTex 根目录执行）：
    python -m pytest -q scripts/batch_excel/test_exporters.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import tempfile
from dataclasses import dataclass

from temporal import normalize_temporal, temporal_field

from openpyxl import load_workbook
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

CATEGORIES = ('personnel', 'materials', 'equipment', 'environment')
PREFIX = ('batch', 'process', 'subprocess', 'step', 'form', 'name')
FORBIDDEN = {'form_order', 'form_index', 'form_sequence', 'instance'}


def scalar(value, path):
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value) > 32767 or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]', value):
            raise ValueError(f'{path}: text cannot be represented safely in Excel')
        return
    if isinstance(value, (int, float)) and math.isfinite(value):
        return
    raise ValueError(f'{path}: expected a finite number, string, boolean or null')


def mapping(value, path, required, allowed):
    if not isinstance(value, dict):
        raise ValueError(f'{path}: expected an object')
    missing = set(required) - value.keys()
    extra = value.keys() - set(allowed)
    if missing or extra:
        raise ValueError(f'{path}: missing keys {sorted(missing)}; unsupported keys {sorted(extra)}')


def validate(data):
    mapping(data, '$', ('batch', 'process'), ('batch', 'process'))
    if not isinstance(data['batch'], str) or not data['batch'].strip():
        raise ValueError('$.batch: expected a nonempty string')
    scalar(data['batch'], '$.batch')
    if flat_schema(data):
        validate_flat(data)
        return

    def children(items, path, child):
        if not isinstance(items, list):
            raise ValueError(f'{path}: expected an array')
        for i, node in enumerate(items):
            location = f'{path}[{i}]'
            allowed = ('name', child) if child else ('name', *CATEGORIES)
            mapping(node, location, allowed if child else ('name',), allowed)
            if not isinstance(node['name'], str):
                raise ValueError(f'{location}.name: expected a string')
            scalar(node['name'], f'{location}.name')
            if child:
                following = {'subprocess': 'step', 'step': 'form', 'form': None}[child]
                children(node[child], f'{location}.{child}', following)
                continue
            for category in CATEGORIES:
                objects = node.get(category, [])
                if not isinstance(objects, list):
                    raise ValueError(f'{location}.{category}: expected an array')
                for j, obj in enumerate(objects):
                    opath = f'{location}.{category}[{j}]'
                    mapping(obj, opath, ('name', 'attributes'), ('name', 'attributes'))
                    if not isinstance(obj['name'], str):
                        raise ValueError(f'{opath}.name: expected a string')
                    scalar(obj['name'], f'{opath}.name')
                    if not isinstance(obj['attributes'], dict):
                        raise ValueError(f'{opath}.attributes: expected an object')
                    for key, value in obj['attributes'].items():
                        if not re.fullmatch('[a-z][a-z0-9_]*', key) or key in FORBIDDEN:
                            raise ValueError(f'{opath}.attributes: unsupported field {key!r}')
                        scalar(value, f'{opath}.attributes.{key}')
    children(data['process'], '$.process', 'subprocess')


def flat_schema(data):
    return isinstance(data.get('process'), list) and bool(data['process']) and isinstance(data['process'][0], dict) and isinstance(data['process'][0].get('subprocess'), str)


def validate_flat(data):
    ids = set()

    def text_fields(node, keys, path):
        for key in keys:
            if not isinstance(node[key], str):
                raise ValueError(f'{path}.{key}: expected a string')
            scalar(node[key], f'{path}.{key}')
        if 'id' in keys:
            if not node['id'].strip() or node['id'] in ids:
                raise ValueError(f'{path}: empty or duplicate id {node["id"]!r}')
            ids.add(node['id'])

    for pi, process in enumerate(data['process']):
        path = f'$.process[{pi}]'
        mapping(process, path, ('subprocess', 'step'), ('subprocess', 'step'))
        text_fields(process, ('subprocess',), path)
        if not isinstance(process['step'], list):
            raise ValueError(f'{path}.step: expected an array')
        for ti, step in enumerate(process['step']):
            location = f'{path}.step[{ti}]'
            mapping(step, location, ('id', 'stage', 'form'), ('id', 'stage', 'form', *CATEGORIES))
            text_fields(step, ('id', 'stage', 'form'), location)
            for category in CATEGORIES:
                objects = step.get(category, [])
                if not isinstance(objects, list):
                    raise ValueError(f'{location}.{category}: expected an array')
                for oi, obj in enumerate(objects):
                    opath = f'{location}.{category}[{oi}]'
                    mapping(obj, opath, ('id', 'name', 'attributes'), ('id', 'name', 'attributes'))
                    text_fields(obj, ('id', 'name'), opath)
                    if not isinstance(obj['attributes'], dict):
                        raise ValueError(f'{opath}.attributes: expected an object')
                    for key, value in obj['attributes'].items():
                        if not re.fullmatch('[a-z][a-z0-9_]*', key) or key in FORBIDDEN:
                            raise ValueError(f'{opath}.attributes: unsupported field {key!r}')
                        scalar(value, f'{opath}.attributes.{key}')


def forms(data):
    """Keep occurrence identity separate from visible names; equal names can be distinct forms."""
    if flat_schema(data):
        for pi, process in enumerate(data['process']):
            for ti, step in enumerate(process['step']):
                unique = {}
                for category in CATEGORIES:
                    seen = set()
                    unique[category] = []
                    for obj in step.get(category, []):
                        signature = json.dumps([obj['name'], obj['attributes']], sort_keys=True, ensure_ascii=False)
                        if signature not in seen:
                            seen.add(signature)
                            unique[category].append(obj)
                yield (pi, 0, ti, 0), [data['batch'], process['subprocess'], step['stage'], step['id'], step['form']], unique
        return
    for pi, process in enumerate(data['process']):
        for si, sub in enumerate(process['subprocess']):
            for ti, step in enumerate(sub['step']):
                for fi, form in enumerate(step['form']):
                    prefix = [data['batch'], process['name'], sub['name'], step['name'], form['name']]
                    unique = {}
                    for category in CATEGORIES:
                        seen = set()
                        unique[category] = []
                        for obj in form.get(category, []):
                            signature = json.dumps(obj, sort_keys=True, ensure_ascii=False)
                            if signature not in seen:
                                seen.add(signature)
                                unique[category].append(obj)
                    yield (pi, si, ti, fi), prefix, unique


@dataclass(frozen=True)
class ExcelTemporal:
    value: dt.datetime | dt.time
    number_format: str


def excel_value(key, value):
    # Identifier-looking strings stay strings, including leading zeros.
    if isinstance(value, int) and not isinstance(value, bool) and len(str(abs(value))) > 15:
        return str(value)
    if temporal_field(key) and isinstance(value, str):
        text, kind = normalize_temporal(value)
        if kind == 'time':
            return ExcelTemporal(dt.time.fromisoformat(text), 'hh:mm:ss' if text.count(':') == 2 else 'hh:mm')
        if kind in {'date', 'datetime'}:
            date = dt.datetime.fromisoformat(text[:10] + ('T' + text[11:] if kind == 'datetime' else ''))
            if date.year >= 1900:
                fmt = 'yyyy-mm-dd'
                if kind == 'datetime':
                    fmt += '-hh:mm:ss' if text.count(':') == 2 else '-hh:mm'
                return ExcelTemporal(date, fmt)
    return value


def append(ws, row):
    if ws.max_row >= 1048576 or len(row) > 16384:
        raise ValueError(f'{ws.title}: Excel row/column limit exceeded')
    ws.append([v.value if isinstance(v, ExcelTemporal) else v for v in row])
    for cell, original in zip(ws[ws.max_row], row):
        if isinstance(cell.value, str):
            cell.data_type = 's'
        if isinstance(cell.value, dt.datetime):
            cell.number_format = 'yyyy-mm-dd'
        if isinstance(original, ExcelTemporal):
            cell.number_format = original.number_format
        cell.alignment = Alignment(vertical='top', wrap_text=True)


def layout(ws, widths, merges=()):
    covered = set()
    for col, start, end in merges:
        ws.merge_cells(start_row=start, end_row=end, start_column=col, end_column=col)
        covered.update((row, col) for row in range(start, end + 1))
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = width

    def lines(value, width):
        text = '' if value is None else str(value)
        return sum(max(1, math.ceil(sum(2 if ord(ch) > 255 else 1 for ch in part) / (width - 2)))
                   for part in text.split('\n'))

    for row in ws:
        height = max([24] + [lines(cell.value, widths[cell.column - 1]) * 16 + 6
                            for cell in row if (cell.row, cell.column) not in covered])
        ws.row_dimensions[row[0].row].height = min(409, height)
    for col, start, end in merges:
        needed = lines(ws.cell(start, col).value, widths[col - 1]) * 16 + 6
        actual = sum(ws.row_dimensions[r].height for r in range(start, end + 1))
        if needed > actual:
            ws.row_dimensions[start].height = min(409, ws.row_dimensions[start].height + needed - actual)
    ws.sheet_view.showGridLines = True


def verify(path, expected):
    wb = load_workbook(path)
    try:
        if wb.sheetnames != list(expected):
            raise ValueError('Workbook sheets changed during export')
        for ws in wb:
            merged = {}
            for region in ws.merged_cells.ranges:
                for r in range(region.min_row, region.max_row + 1):
                    merged[r, region.min_col] = ws.cell(region.min_row, region.min_col).value
            if ws.max_row != len(expected[ws.title]):
                raise ValueError(f'{ws.title}: row count changed during export')
            for ri, row in enumerate(expected[ws.title], 1):
                for ci, value in enumerate(row, 1):
                    temporal = value if isinstance(value, ExcelTemporal) else None
                    if temporal:
                        value = temporal.value
                    cell = ws.cell(ri, ci)
                    actual = merged.get((ri, ci), cell.value)
                    wanted = None if value == '' else value
                    same = actual == wanted
                    if isinstance(actual, float) and isinstance(wanted, float):
                        same = math.isclose(actual, wanted, rel_tol=1e-15, abs_tol=0)
                    if not same or cell.data_type == 'f' or (temporal and cell.number_format != temporal.number_format):
                        raise ValueError(f'{ws.title}!{cell.coordinate}: export readback mismatch')
    finally:
        wb.close()


def save(wb, expected, target, overwrite=False):
    target = Path(target)
    if target.exists() and not overwrite:
        raise FileExistsError(f'{target} already exists; use --overwrite to replace it')
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.xlsx', delete=False) as handle:
        temporary = Path(handle.name)
    try:
        wb.save(temporary)
        verify(temporary, expected)
        if overwrite:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
        wb.close()
    return target


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON key: {key!r}')
        result[key] = value
    return result


def cli(builder, kind, argv, input_json, output_dir):
    parser = argparse.ArgumentParser(description=f'Export English batch JSON as {kind} Excel.')
    parser.add_argument('input_json', nargs='?', type=Path, default=input_json,
                        help='JSON file; overrides INPUT_JSON at the top of the script')
    parser.add_argument('--output-dir', type=Path, default=output_dir,
                        help='Default: directory containing the input JSON')
    parser.add_argument('--overwrite', action='store_true', help='Explicitly replace an existing output')
    args = parser.parse_args(argv)
    if args.input_json is None:
        parser.error('provide input_json or set INPUT_JSON at the top of the script')
    try:
        source = Path(args.input_json).expanduser().resolve()
        data = json.loads(source.read_text(encoding='utf-8-sig'), object_pairs_hook=unique_pairs)
        validate(data)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', data['batch']):
            raise ValueError('batch must be a filesystem-safe identifier (letters, digits, _, ., -)')
        directory = Path(args.output_dir).expanduser().resolve() if args.output_dir else source.parent
        target = directory / f'{data["batch"]}_{kind}.xlsx'
        if target.exists() and not args.overwrite:
            raise FileExistsError(f'{target} already exists; use --overwrite to replace it')
        wb, expected = builder(data)
        save(wb, expected, target, args.overwrite)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(target)
    return target
