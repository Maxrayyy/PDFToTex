"""用途：统一完整日期和时间，保留原始精度；残缺、非法或复合值原样返回。

本模块由 common.py 和批次 JSON 转换脚本调用，无需单独执行或配置路径。
复制 Excel 导出工具时，请将本文件与 common.py 一起复制。
"""

import datetime as dt
import re

DATE = r'(?P<year>\d{4})\s*[年./-]\s*(?P<month>\d{1,2})\s*[月./-]\s*(?P<day>\d{1,2})(?:\s*日)?'
TIME = r'(?P<hour>\d{1,2})\s*[:：时]\s*(?P<minute>\d{2})(?:(?:\s*[:：]|\s*分\s*)(?P<second>\d{2})\s*秒?|\s*分)?'


def temporal_field(key):
    return key in {'date', 'time', 'datetime', 'valid_until', 'effective_until', 'validity'} or key.endswith(
        ('_date', '_time', '_datetime', '_valid_until', '_validity'))


def normalize_temporal(value):
    if not isinstance(value, str):
        return value, 'text'
    for pattern, kind in [(DATE + r'(?:\s*[-T]\s*|\s*)' + TIME, 'datetime'),
                          (DATE, 'date'), (TIME, 'time')]:
        match = re.fullmatch(r'\s*' + pattern + r'\s*', value)
        if not match:
            continue
        parts = match.groupdict()
        try:
            date = dt.date(*(int(parts[k]) for k in ('year', 'month', 'day'))) if kind != 'time' else None
            time = dt.time(int(parts['hour']), int(parts['minute']), int(parts['second'] or 0)) if kind != 'date' else None
        except ValueError:
            return value, 'text'
        clock = time.isoformat(timespec='seconds' if parts.get('second') is not None else 'minutes') if time else ''
        return (date.isoformat() + ('-' + clock if clock else '') if date else clock), kind
    return value, 'text'
