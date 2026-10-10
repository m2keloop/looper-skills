"""Offline A-share trading-day guard for the daily close review."""
import json
from datetime import date
from pathlib import Path

CALENDAR_PATH = Path(__file__).resolve().parents[1] / 'data' / 'a-share-trading-calendar.json'


class CalendarUnavailable(ValueError):
    """The date cannot be confirmed from the saved exchange calendar."""


def trading_day_status(day, calendar_path=CALENDAR_PATH):
    if day.weekday() >= 5:
        return False, '周末休市（调休工作日也不作为证券交易日）'
    try:
        calendar = json.loads(Path(calendar_path).read_text(encoding='utf-8'))
        if calendar.get('schema_version') != 1:
            raise ValueError('交易日历版本无效')
        entry = calendar['years'].get(str(day.year))
        if not entry:
            raise ValueError(f'缺少 {day.year} 年交易所休市日历')
        holidays = entry['holidays']
        if not isinstance(holidays, list) or not entry.get('sources'):
            raise ValueError('交易日历缺少休市日期或官方来源')
        if any(date.fromisoformat(value).year != day.year for value in holidays):
            raise ValueError('休市日期与日历年份不一致')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise CalendarUnavailable(f'无法确认交易日，暂停联网采集：{error}') from error
    if day.isoformat() in holidays:
        return False, '交易所节假日休市'
    return True, 'A 股交易日'
