# -*- coding: utf-8 -*-
"""Kanoon dashboard local server; Python standard library only.

Reads the selected worksheet live from XLSX without changing the workbook.
The browser re-fetches /api/state every 3 seconds. Valid changes to the
configured Excel file are imported automatically unless a presentation is frozen.
"""
import argparse
import math
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
import json
import os
import posixpath
import subprocess
import re
import sys
import webbrowser
from datetime import datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Timer
from urllib.parse import urlsplit, parse_qs
from xml.etree import ElementTree as ET
from zipfile import ZipFile, BadZipFile

BASE_DIR = Path(__file__).resolve().parent
# Persistent files are external to the replaceable source / release ZIP.
DATA_DIR = Path(os.environ.get('LOCAL_OPS_DATA_DIR', str(
    Path(os.environ['LOCALAPPDATA']) / 'LocalOpsWorkbench' if os.name == 'nt' and 'LOCALAPPDATA' in os.environ
    else Path.home() / '.local' / 'share' / 'local-ops-workbench'))).expanduser().resolve()
CONFIG_PATH = DATA_DIR / 'config.private.json'

def load_private_config():
    if not CONFIG_PATH.is_file():
        return {}
    data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Private configuration must be a JSON object')
    return data

PRIVATE_CONFIG = load_private_config()
SHEET_NAME = 'آبان 1405'
EXCEL_FILE = 'آبان 1405(3).xlsx'
INTELLIGENCE_ELIGIBLE = tuple(PRIVATE_CONFIG.get('intelligence_eligible', ()))
HOST, PORT = '127.0.0.1', int(os.environ.get('LOCAL_OPS_PORT','8765'))
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'p': 'http://schemas.openxmlformats.org/package/2006/relationships'}
CELL_REF = re.compile(r'^([A-Z]+)(\d+)$')


def excel_col(reference):
    match = CELL_REF.match(reference)
    if not match:
        raise ValueError('Bad Excel cell reference: ' + reference)
    num = 0
    for ch in match.group(1):
        num = num * 26 + ord(ch) - 64
    return num - 1


def read_strings(z):
    if 'xl/sharedStrings.xml' not in z.namelist():
        return []
    root = ET.fromstring(z.read('xl/sharedStrings.xml'))
    return [''.join(x.text or '' for x in item.findall('.//s:t', NS))
            for item in root.findall('s:si', NS)]


def read_cells(z, sheet_name):
    workbook = ET.fromstring(z.read('xl/workbook.xml'))
    relationships = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
    targets = {rel.get('Id'): rel.get('Target') for rel in relationships}
    path = None
    available = []
    for sheet in workbook.findall('.//s:sheets/s:sheet', NS):
        available.append(sheet.get('name'))
        if sheet.get('name') == sheet_name:
            target = targets.get(sheet.get('{%s}id' % NS['r']))
            if not target:
                raise ValueError('Cannot resolve worksheet XML for ' + sheet_name)
            path = target.lstrip('/') if target.startswith('/') else posixpath.normpath(posixpath.join('xl', target))
    if not path:
        raise ValueError('Worksheet %s not found (available: %s)' % (sheet_name, ', '.join(available)))
    strings = read_strings(z)
    sheet_xml = ET.fromstring(z.read(path))
    rows = {}
    for cell in sheet_xml.findall('.//s:sheetData/s:row/s:c', NS):
        address = cell.get('r', '')
        match = CELL_REF.match(address)
        if not match:
            continue
        ri = int(match.group(2)) - 1
        ci = excel_col(address)
        if ci > 60 or ri > 5000:
            continue
        typ = cell.get('t')
        val = cell.find('s:v', NS)
        if typ == 'inlineStr':
            ins = cell.find('s:is', NS)
            value = ''.join(t.text or '' for t in ins.findall('.//s:t', NS)) if ins is not None else ''
        elif val is None or val.text is None or typ == 'e':
            value = None
        elif typ == 's':
            value = strings[int(val.text)]
        elif typ == 'str':
            value = val.text
        elif typ == 'b':
            value = val.text == '1'
        else:
            try:
                value = float(val.text)
            except ValueError:
                value = val.text
        rows.setdefault(ri, {})[ci] = value
    return rows



# Private, read-only supporter source. This workbook supplies display detail only;
# it must not affect the evaluation/SQLite scoring lifecycle.
SUPPORTERS_FILE = 'Shiraz_Supporters_With_Video_View_Percentage.xlsx'
SUPPORTERS_CONFIG = PRIVATE_CONFIG.get('supporters', {})
SUPPORTERS_SHA256 = SUPPORTERS_CONFIG.get('sha256', '')
SUPPORTER_MANAGER_ALIAS = SUPPORTERS_CONFIG.get('manager_alias', {})
SUPPORTER_EXCLUDED = set(SUPPORTERS_CONFIG.get('excluded', ()))
SUPPORTER_METRICS = (
    ('درصد حضور در حوزه', 3), ('درصد حضور کل', 4),
    ('کتاب', 5), ('فیلم پیشرفت', 6),
)
SUPPORTER_SHEETS = tuple(SUPPORTERS_CONFIG.get('sheets', ()))


def read_supporters(path=None):
    """Validate exact private source identity; preserve raw values and provenance."""
    file_path = Path(path) if path is not None else DATA_DIR / SUPPORTERS_FILE
    if not SUPPORTERS_SHA256 or not SUPPORTER_SHEETS:
        raise ValueError('Private supporters source configuration is missing')
    source_bytes = file_path.read_bytes()
    fingerprint = sha256(source_bytes).hexdigest()
    if fingerprint != SUPPORTERS_SHA256:
        raise ValueError('Supporters workbook does not match the approved SHA-256')
    records = []
    issues = []
    import io
    with ZipFile(io.BytesIO(source_bytes)) as wb:
        if wb.testzip() is not None:
            raise ValueError('Damaged supporters workbook')
        for sheet in SUPPORTER_SHEETS:
            rows = read_cells(wb, sheet)
            header = rows.get(0, {})
            if [header.get(col) for col in range(7)] != [
                'ردیف', 'نام مدیر', 'نام پشتیبان', 'میانگین درصد حضوری',
                'میانگین درصد کل', 'درصد فروش کتاب', 'درصد مشاهده فیلم',
            ]:
                raise ValueError('Unexpected supporters columns in ' + sheet)
            manager = SUPPORTER_MANAGER_ALIAS.get(sheet, sheet)
            for row_index, row in sorted(rows.items()):
                if row_index == 0:
                    continue
                if row.get(2) is None:
                    continue
                if row.get(1) != sheet:
                    raise ValueError('Supporter manager mismatch in sheet ' + sheet)
                name = row.get(2)
                if not isinstance(name, str) or not name.strip():
                    raise ValueError('Invalid supporter name')
                values = {}
                for metric, col in SUPPORTER_METRICS:
                    raw = row.get(col)
                    if raw is not None and (not isinstance(raw, (int, float)) or
                             isinstance(raw, bool) or not math.isfinite(raw) or
                             raw < 0 or raw > 1):
                        raise ValueError('Unsupported supporter percentage: %s row %s col %s' % (sheet, row_index+1, col+1))
                    substituted = col == 6 and raw is None
                    values[metric] = {
                        'raw': raw,
                        'effective': 0 if substituted else (raw * 100 if raw is not None else None),
                        'status': 'blank_g_zero' if substituted else 'missing' if raw is None else 'scored',
                        'unit': '%', 'cell': chr(ord('A') + col) + str(row_index + 1),
                    }
                records.append({
                    'manager': manager, 'managerSheet': sheet, 'name': name.strip(),
                    'row': row_index + 1, 'values': values,
                })
        # Preserve recorded source discrepancies without choosing a competing reading.
        for idx, row in sorted(read_cells(wb, 'کنترل مشاهده فیلم').items()):
            if row.get(2) == 'دو مقدار متفاوت برای یک فرد':
                issues.append({
                    'manager': SUPPORTER_MANAGER_ALIAS.get(row.get(0), row.get(0)),
                    'supporter': row.get(1), 'reason': row.get(2),
                    'source': row.get(4), 'note': row.get(9),
                })
    scoped = [item for item in records if item['manager'] not in SUPPORTER_EXCLUDED]
    if len(records) != 63 or len(scoped) != 61 or len({r['manager'] for r in scoped}) != 9:
        raise ValueError('Supporter identity/count drift')
    if sum(r['values']['فیلم پیشرفت']['status'] == 'blank_g_zero' for r in records) != 16:
        raise ValueError('Supporter G-blank contract drift')
    if len(issues) != 2:
        raise ValueError('Recorded source discrepancy count drift')
    return {
        'sourceSha256': fingerprint, 'metrics': [m for m, _ in SUPPORTER_METRICS],
        'records': scoped, 'issues': issues,
        'excludedManager': next(iter(SUPPORTER_EXCLUDED), None),
    }

def required_number(value, label):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError('Non-numeric Excel value in ' + label)
    if not (float('-inf') < value < float('inf')):
        raise ValueError('Non-finite Excel value in ' + label)
    return float(value)


def extract_workbook(path, sheet_name=SHEET_NAME):
    with ZipFile(path, 'r') as z:
        cells = read_cells(z, sheet_name)
    header = cells.get(0, {})
    if str(header.get(0, '')).strip() != 'عملکرد' or str(header.get(1, '')).strip() != 'ضرائب':
        raise ValueError('Unexpected worksheet header: expected عملکرد / ضرائب in A1 and B1')
    columns = [i for i in range(2, 61) if header.get(i) is not None and str(header.get(i)).strip()]
    if not columns:
        raise ValueError('No manager columns found in row 1')
    if columns != list(range(2, columns[-1] + 1)):
        raise ValueError('Manager columns contain gaps')
    managers = [str(header[c]).strip() for c in columns]
    if len(managers) != len(set(managers)):
        raise ValueError('Duplicate manager names in selected sheet; cannot identify them reliably')
    metrics = []
    source_issues = []
    source_scores = None
    source_ranks = None
    for ri in sorted(cells.keys()):
        if ri == 0:
            continue
        row = cells[ri]
        name = str(row.get(0, '') or '').strip()
        if not name:
            continue
        if name == 'درصد نهایی':
            source_scores = [row.get(c) for c in columns]
            continue
        if name == 'رتبه':
            source_ranks = [row.get(c) for c in columns]
            continue
        weight = row.get(1)
        missing_weight = False
        if weight is None:
            # The updated workbook contains one explicitly named but still unweighted
            # empty metric. Keep it visible with effective weight zero, never invent
            # a coefficient or silently discard the row.
            if name == 'امتحان' and all(row.get(c) is None for c in columns):
                missing_weight = True
                source_issues.append('معیار «امتحان» در Excel ضریب ندارد؛ تا تعیین ضریب در داشبورد، با ضریب مؤثر صفر محاسبه می‌شود.')
                weight = 0
            else:
                raise ValueError('Missing metric weight in Excel at B%s: %s' % (ri + 1, name))
        weight = required_number(weight, name + ' weight')
        if weight < 0:
            raise ValueError('Negative metric weight: ' + name)
        values = []
        for c in columns:
            value = row.get(c)
            if value is None:
                values.append(None)
            else:
                num = required_number(value, name + ', column ' + str(c+1))
                values.append(num)
        metrics.append({'row': ri + 1, 'name': name, 'weight': weight, 'values': values, 'type':'raw', 'weightMissing':missing_weight})
    if not metrics:
        raise ValueError('No weighted metrics found')
    if len({m['name'] for m in metrics}) != len(metrics):
        raise ValueError('Duplicate metric names in the worksheet; stable identity would be ambiguous')
    if not any(m['weight'] > 0 for m in metrics):
        raise ValueError('All metric weights are zero')
    if 'کلاس هوش' not in {m['name'] for m in metrics}:
        raise ValueError('Expected metric کلاس هوش is missing; applicability rule cannot be enforced')
    if not INTELLIGENCE_ELIGIBLE:
        raise ValueError('Private intelligence-applicability configuration is missing')
    if not set(INTELLIGENCE_ELIGIBLE).issubset(set(managers)):
        raise ValueError('مدیران مشمول کلاس هوش در فایل Excel به طور کامل شناسایی نشدند.')
    result = {'sheet': sheet_name, 'fileName': path.name,
              'label': 'ارزیابی آبان ۱۴۰۵ · داده‌های Excel و SQLite', 'managers': managers,
              'metrics': metrics, 'sourceScores': source_scores, 'sourceRanks': source_ranks,
              'sourceIssues': source_issues,
              'applicability': {'کلاس هوش': list(INTELLIGENCE_ELIGIBLE)}}
    return result



# SQLite is the durable working record for the selected evaluation period.
# Excel remains read-only input, imported automatically when the presentation is not frozen.
DB_PATH = DATA_DIR / 'dashboard.sqlite3'
LOCK = threading.RLock()
MAX_BODY = 1024 * 1024
DEFAULT_SETTINGS = {'version': 1, 'excluded': [], 'added': [], 'weights': {}, 'cells': {}}

@contextmanager
def db_connection(path=None):
    """Transaction context that also closes the SQLite handle (important on Windows)."""
    db=sqlite3.connect(str(path) if path is not None else DB_PATH, timeout=8)
    try:
        with db:
            yield db
    finally:
        db.close()



def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def fingerprint(payload):
    return sha256(compact(payload).encode('utf-8')).hexdigest()


def period_key(filename, sheet):
    return filename + ' | ' + sheet


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_connection() as db:
        db.execute('PRAGMA busy_timeout=8000')
        db.execute('''CREATE TABLE IF NOT EXISTS evaluations (
            period_id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            sheet TEXT NOT NULL,
            source_json TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0,
            frozen INTEGER NOT NULL DEFAULT 0,
            pending_json TEXT,
            pending_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )''')
        db.commit()


def now_iso():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def db_row(db, period_id):
    db.row_factory = sqlite3.Row
    return db.execute('SELECT * FROM evaluations WHERE period_id=?', (period_id,)).fetchone()


def ensure_row(db, period_id, filename, sheet, excel_data=None):
    row = db_row(db, period_id)
    if row is None and excel_data is not None:
        text = compact(excel_data)
        instant = now_iso()
        db.execute('''INSERT INTO evaluations
            (period_id,filename,sheet,source_json,source_hash,settings_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)''', (period_id, filename, sheet, text, fingerprint(excel_data), compact(DEFAULT_SETTINGS), instant, instant))
        row = db_row(db, period_id)
    return row


def valid_number(v, *, min_value=0, max_value=1000, integer=False):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and min_value <= v <= max_value and (not integer or (isinstance(v, int) or v.is_integer()))


def metric_id(name):
    # Same name across row reorder retains identity; rename requires explicit reconciliation.
    return 'xlsname:' + name.strip()


def migrate_legacy_id(value, source):
    if not isinstance(value, str):
        raise ValueError('Invalid metric identifier')
    if value.startswith('local:'):
        return value
    if value.startswith('xlsname:'):
        return value
    if value.startswith('xls:'):
        # v1.3 encoded xls:<row>:<name>, but current stable IDs use a name hash.
        if value.count(':') >= 2:
            return metric_id(value.split(':', 2)[2])
        return value
    raise ValueError('Unexpected metric identifier')


def validate_settings(raw, source):
    if not isinstance(raw, dict) or raw.get('version') != 1:
        raise ValueError('Unsupported settings version')
    ex, add, weights = raw.get('excluded'), raw.get('added'), raw.get('weights')
    if not isinstance(ex, list) or not isinstance(add, list) or not isinstance(weights, dict):
        raise ValueError('Incomplete settings structure')
    if len(add) > 50:
        raise ValueError('حداکثر ۵۰ معیار سفارشی مجاز است.')
    manager_names = set(source['managers'])
    base_names = {m['name'] for m in source['metrics']}
    ids, names, clean_add = set(), set(base_names), []
    for m in add:
        if not isinstance(m, dict):
            raise ValueError('Malformed custom metric')
        mid, title, kind, weight, values = (m.get(x) for x in ('id','name','type','weight','valuesByManager'))
        if not isinstance(mid,str) or not mid.startswith('local:') or len(mid)>90 or mid in ids:
            raise ValueError('Invalid or duplicate custom metric ID')
        if not isinstance(title,str) or not 2 <= len(title.strip()) <= 80 or title.strip() in names:
            raise ValueError('Invalid or duplicate metric name: ' + str(title))
        if kind not in ('percent','score','count') or not valid_number(weight, integer=True) or not isinstance(values,dict):
            raise ValueError('Invalid custom metric properties: ' + title)
        if len(values) > 200:
            raise ValueError('Too many manager values')
        cleaned={}
        for manager, val in values.items():
            if not isinstance(manager,str) or len(manager)>120 or manager not in manager_names:
                # Preserve name-linked historical values even if manager was removed/renamed;
                # never silently assign their data to a different manager.
                if not isinstance(manager,str) or not 1 <= len(manager) <= 120:
                    raise ValueError('Invalid manager value key')
            if val is None:
                continue
            if kind=='count':
                if not valid_number(val, max_value=9007199254740991, integer=True):
                    raise ValueError('Count must be a nonnegative safe integer: '+title)
            elif not valid_number(val, max_value=100):
                raise ValueError('Percentage/score is outside 0-100: '+title)
            cleaned[manager]=val
        ids.add(mid);names.add(title.strip())
        clean_add.append({'id':mid,'name':title.strip(),'type':kind,'weight':int(weight),'valuesByManager':cleaned})
    known = {metric_id(m['name']) for m in source['metrics']} | ids
    if len(ex) > 200 or len(weights)>300:
        raise ValueError('Too many stored settings')
    clean_ex=[]
    for v in ex:
        fixed=migrate_legacy_id(v,source)
        if fixed not in clean_ex: clean_ex.append(fixed)
    clean_weights={}
    for k,v in weights.items():
        fixed=migrate_legacy_id(k,source)
        if not valid_number(v,integer=True):
            raise ValueError('Invalid metric weight')
        clean_weights[fixed]=int(v)
    cells = raw.get('cells', {})
    if not isinstance(cells, dict) or len(cells) > 100:
        raise ValueError('ساختار وضعیت مدیر/معیار معتبر نیست.')
    normalized_cells = {}
    for incoming_id, manager_values in cells.items():
        mid = migrate_legacy_id(incoming_id, source)
        if not isinstance(manager_values, dict) or len(manager_values) > 250:
            raise ValueError('ساختار مقادیر معیار نامعتبر است.')
        this = {}
        for manager, cell in manager_values.items():
            if not isinstance(manager, str) or not 1 <= len(manager) <= 120:
                raise ValueError('شناسه مدیر معتبر نیست.')
            if not isinstance(cell, dict) or cell.get('status') not in ('scored', 'missing', 'na'):
                raise ValueError('وضعیت مدیر/معیار معتبر نیست.')
            status = cell['status']
            if mid == metric_id('کلاس هوش') and manager in manager_names and manager not in source.get('applicability', {}).get('کلاس هوش', []):
                if status != 'na':
                    raise ValueError('کلاس هوش فقط برای سه مدیر مشخص‌شده قابل محاسبه است؛ برای '+manager+' وضعیت نامشمول لازم است.')
            if status == 'scored':
                value = cell.get('value')
                if value is not None and not valid_number(value, max_value=9007199254740991):
                    raise ValueError('مقدار نمره معتبر نیست.')
                this[manager] = {'status': status, 'value': value}
            else:
                this[manager] = {'status': status}
        if this: normalized_cells[mid] = this
    return {'version':1,'excluded':clean_ex,'added':clean_add,'weights':clean_weights, 'cells':normalized_cells}


def unresolved_warnings(settings, source):
    known={metric_id(m['name']) for m in source['metrics']} | {m['id'] for m in settings['added']}
    orphan=sorted((set(settings['excluded']) | set(settings['weights']) | set(settings.get('cells',{}))) - known)
    managers=set(source['managers'])
    missing_names=sorted(({name for m in settings['added'] for name in m['valuesByManager'] if name not in managers}) | ({name for group in settings.get('cells',{}).values() for name in group if name not in managers}))
    items=[]
    if orphan:
        items.append('برخی تنظیمات معیارها به معیار فعلی Excel متصل نیستند؛ احتمالاً نام معیار تغییر کرده است. بدون تطبیق دستی اعمال نشده‌اند: '+', '.join(orphan[:3]))
    if missing_names:
        items.append('مقادیر ثبت‌شده برای نام‌های قدیمی/ناموجود محفوظ‌اند اما به مدیر دیگری منتقل نشده‌اند: '+', '.join(missing_names[:4]))
    return items


def resolve_cell(settings, mid, manager, base_value, *, custom=False):
    """One explicit state per metric/manager; unspecified uses Excel or legacy custom value."""
    override = settings.get('cells', {}).get(mid, {}).get(manager)
    if override is not None:
        if override['status'] == 'na': return 'na', None
        if override['status'] == 'missing': return 'missing', None
        if override.get('value') is None: return 'missing', None
        return 'scored', override['value']
    return ('missing', None) if base_value is None else ('scored', base_value)


def readiness_error(source, settings, *, already_validated=False):
    """Recalculate from one SQLite transaction; N/A removes its coefficient from the denominator."""
    if not already_validated:
        settings = validate_settings(settings, source)
    managers = source['managers']
    if not managers or not source['metrics']:
        return 'مدیر یا معیار ارزیابی موجود نیست.'
    excluded = set(settings['excluded'])
    active = []
    for m in source['metrics']:
        mid = metric_id(m['name'])
        if mid not in excluded:
            active.append((m['name'], mid, settings['weights'].get(mid, m['weight']), m['values']))
    for m in settings['added']:
        mid = m['id']
        if mid not in excluded:
            active.append((m['name'], mid, settings['weights'].get(mid, m['weight']),
                           [m['valuesByManager'].get(n) for n in managers]))
    if not active or not any(w > 0 for _,_,w,_ in active):
        return 'حداقل یک معیار با ضریب مثبت لازم است.'
    eligible_by_id = {metric_id(title): set(eligible) for title, eligible in source.get('applicability', {}).items()}
    for i, name in enumerate(managers):
        numerator, denominator = [], []
        for title, mid, weight, values in active:
            if weight <= 0: continue
            if mid in eligible_by_id and name not in eligible_by_id[mid]:
                continue  # Not applicable: removes this metric's weight from denominator
            status, value = resolve_cell(settings, mid, name, values[i])
            if status == 'na': continue
            if status == 'missing':
                return 'نمره معیار «' + title + '» برای «' + name + '» ثبت نشده است.'
            if not valid_number(value, max_value=9007199254740991):
                return 'نمره معیار «' + title + '» برای «' + name + '» نامعتبر است.'
            part = weight * value
            if not math.isfinite(part):
                return 'حاصل‌ضرب نمره معیار «' + title + '» نامعتبر است.'
            numerator.append(part)
            denominator.append(weight)
        weight_total = math.fsum(denominator)
        if not math.isfinite(weight_total) or weight_total <= 0:
            return 'هیچ معیار دارای ضریب مثبت و قابل اعمال برای «' + name + '» باقی نمانده است.'
        result = math.fsum(numerator)/weight_total
        if not math.isfinite(result):
            return 'امتیاز نهایی «' + name + '» معتبر نیست.'
    # Only compare to Excel's cached formula results if its unmodified raw cells
    # actually contain a complete baseline; an empty template with cached zeros
    # is not a validated score reference.
    raw_complete = all(m['weight'] <= 0 or all(valid_number(v,max_value=9007199254740991)
                        for v in m['values']) for m in source['metrics'])
    if raw_complete:
        refs, ranks = source.get('sourceScores'), source.get('sourceRanks')
        if isinstance(refs,list) and isinstance(ranks,list) and len(refs)==len(managers) and len(ranks)==len(managers):
            denom = math.fsum(m['weight'] for m in source['metrics'])
            if denom > 0:
                scores=[]
                for i in range(len(managers)):
                    scores.append(math.fsum(m['weight']*m['values'][i] for m in source['metrics'])/denom)
                for i,name in enumerate(managers):
                    rank=1+sum(1 for x in scores if x>scores[i]+1e-9)
                    if not valid_number(refs[i],max_value=9007199254740991) or abs(refs[i]-scores[i])>.021 or not valid_number(ranks[i],min_value=1,max_value=len(managers),integer=True) or int(ranks[i])!=rank:
                        return 'مغایرت با نتیجه ثبت‌شده در Excel برای «'+name+'».'
    return None



def readiness_report(source, settings):
    """Explain every missing manager/metric cell while honoring the canonical gate.

    This is presentation diagnostics only. The authoritative verdict remains
    readiness_error(), which is also evaluated atomically by freeze().
    """
    try:
        settings = validate_settings(settings, source)
    except (ValueError, TypeError, KeyError) as exc:
        return {'ready': False, 'reason': 'تنظیمات ذخیره‌شده معتبر نیستند: '+str(exc),
                'missingCount':0,'incompleteManagers':len(source.get('managers',[])),
                'groups':[],'noDenominator':[],
                'otherIssues':['تنظیمات SQLite ناسازگار است؛ از فایل پایگاه داده پشتیبان بگیرید و بدون بازنویسی آن مشکل را بررسی کنید.'],
                'activeMetricCount':0,'scaleWarnings':[]}
    verdict = readiness_error(source, settings, already_validated=True)
    excluded = set(settings['excluded'])
    managers = source['managers']
    groups = []
    total_missing = 0
    incomplete_people = set()
    effective = {manager: 0.0 for manager in managers}
    expected = {manager: 0 for manager in managers}
    for metric in list(source['metrics']) + list(settings['added']):
        is_custom = 'id' in metric and str(metric['id']).startswith('local:')
        mid = metric['id'] if is_custom else metric_id(metric['name'])
        if mid in excluded:
            continue
        weight = settings['weights'].get(mid, metric['weight'])
        if weight <= 0:
            continue
        applicable = source.get('applicability', {}).get(metric['name'])
        missing = []
        for i, manager in enumerate(managers):
            if applicable is not None and manager not in applicable:
                continue
            base = metric['valuesByManager'].get(manager) if is_custom else metric['values'][i]
            state, value = resolve_cell(settings, mid, manager, base)
            if state == 'na':
                continue
            effective[manager] += weight
            expected[manager] += 1
            if state != 'scored' or value is None or not valid_number(value, max_value=9007199254740991):
                missing.append(manager)
                incomplete_people.add(manager)
                total_missing += 1
        if missing:
            groups.append({'metricId': mid, 'metric': metric['name'],
                           'weight': weight, 'managers': missing, 'count': len(missing)})
    no_denominator = [manager for manager in managers if effective[manager] <= 0]
    incomplete_people.update(no_denominator)
    # If other independent guards fail (e.g. source cached formula mismatch),
    # the first canonical error must still be visible.
    other = []
    if verdict and not groups and not no_denominator:
        other.append(verdict)
    scale_warnings=[]
    for metric in source['metrics']:
        mid=metric_id(metric['name'])
        if mid in excluded or settings['weights'].get(mid, metric['weight'])<=0: continue
        high=[]
        for i, manager in enumerate(managers):
            if source.get('applicability',{}).get(metric['name']) is not None and manager not in source['applicability'][metric['name']]: continue
            st,val=resolve_cell(settings,mid,manager,metric['values'][i])
            if st=='scored' and val is not None and val>100: high.append(manager)
        if high: scale_warnings.append('مقدار معیار «'+metric['name']+'» برای '+', '.join(high[:3])+(' و سایرین' if len(high)>3 else '')+' از ۱۰۰ بیشتر است؛ مقیاس این معیار را بررسی کنید (بدون تغییر خودکار نمره).')
    return {'ready': verdict is None, 'reason': verdict,
            'missingCount': total_missing, 'incompleteManagers': len(incomplete_people),
            'groups': groups, 'noDenominator': no_denominator,
            'otherIssues': other, 'scaleWarnings': scale_warnings, 'activeMetricCount': sum(1 for x in list(source['metrics']) + list(settings['added'])
                    if (x['id'] if 'id' in x and str(x['id']).startswith('local:') else metric_id(x['name'])) not in excluded
                    and settings['weights'].get(x['id'] if 'id' in x and str(x['id']).startswith('local:') else metric_id(x['name']), x['weight']) > 0)}


class Dashboard:
    def __init__(self, path, sheet):
        self.path=path
        self.sheet=sheet
        self.filename=path.name
        self.period_id=period_key(self.filename,sheet)
        init_db()
        self.refresh()
        with db_connection() as db:
            if db_row(db,self.period_id) is None:
                raise RuntimeError('برای این دوره داده Excel معتبر یا نسخه واردشده در پایگاه داده وجود ندارد.')

    def refresh(self):
        with LOCK:
            excel, warning=None, None
            try:
                excel=extract_workbook(self.path,self.sheet)
            except (OSError, BadZipFile, ET.ParseError, ValueError, KeyError) as exc:
                warning=str(exc)
            with db_connection() as db:
                row=ensure_row(db,self.period_id,self.filename,self.sheet,excel)
                if not row: return {'warning':warning,'excel_status':'missing','error':'No DB snapshot'}
                if excel is not None:
                    digest=fingerprint(excel)
                    if digest == row['source_hash']:
                        if row['pending_json'] is not None:
                            db.execute('UPDATE evaluations SET pending_json=NULL,pending_hash=NULL WHERE period_id=?',(self.period_id,))
                    elif row['frozen'] or row['pending_json'] is not None:
                        db.execute('UPDATE evaluations SET pending_json=?,pending_hash=? WHERE period_id=?', (compact(excel),digest,self.period_id))
                    else:
                        db.execute('UPDATE evaluations SET source_json=?,source_hash=?,updated_at=? WHERE period_id=?',(compact(excel),digest,now_iso(),self.period_id))
                    db.commit()
                return {'warning':warning, 'excel_status':'connected' if excel is not None else 'unavailable'}

    def _state_from_row(self, row, status):
        data=json.loads(row['source_json'])
        settings=json.loads(row['settings_json'])
        report=readiness_report(data,settings)
        settings_error=None
        if report['reason'] and report['reason'].startswith('تنظیمات ذخیره‌شده معتبر نیستند:'):
            settings_error=report['reason']
            settings=DEFAULT_SETTINGS  # display-only, API still rejects invalid stored settings; no DB overwrite
        return {'data':data,'settings':settings,'readiness':report,'settingsInvalid':settings_error,'revision':row['revision'],
                'sourceHash':row['source_hash'], 'periodId':self.period_id,
                'frozen':bool(row['frozen']),'pending':bool(row['pending_json']),
                'excelStatus':status.get('excel_status'),'warning':status.get('warning'),
                'settingsWarnings':unresolved_warnings(settings,data),
                'updatedAt':row['updated_at']}

    def state(self):
        with LOCK:
            status=self.refresh()
            with db_connection() as db:
                row=db_row(db,self.period_id)
                if row is None: raise ValueError('No saved evaluation period')
                return self._state_from_row(row,status)

    def save_settings(self,payload):
        if not isinstance(payload,dict) or type(payload.get('revision')) is not int:
            raise ValueError('Missing optimistic-lock revision')
        with LOCK,db_connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db_row(db,self.period_id)
            if payload['revision'] != row['revision']:
                raise Conflict('تنظیمات در جای دیگری تغییر کرده‌اند؛ صفحه را تازه‌سازی کنید.')
            source=json.loads(row['source_json'])
            value=validate_settings(payload.get('settings'),source)
            if row['frozen']:
                # Narrow frozen exception: only a change to the existing weights map.
                # Comparing the entire canonical settings document also rules out a
                # disguised score, applicability, or custom-metric edit in this POST.
                if payload.get('weightsOnly') is not True:
                    raise Conflict('ارزیابی قفل است؛ فقط تغییر ضریب در بخش مدیریت ضرایب مجاز است.')
                before=validate_settings(json.loads(row['settings_json']),source)
                if any(value[key]!=before[key] for key in ('version','excluded','added','cells')):
                    raise Conflict('در دوره قفل‌شده فقط تغییر ضریب مجاز است؛ نمره‌ها و معیارها نباید تغییر کنند.')
                known={metric_id(m['name']) for m in source['metrics']} | {m['id'] for m in before['added']}
                if any(mid not in known for mid in value['weights']):
                    raise ValueError('شناسه ضریب در این دوره شناخته نشده است.')
            elif payload.get('weightsOnly') not in (None,False,True):
                raise ValueError('نوع عملیات ذخیره معتبر نیست.')
            newrev=row['revision']+1
            db.execute('UPDATE evaluations SET settings_json=?,revision=?,updated_at=? WHERE period_id=?',
                       (compact(value),newrev,now_iso(),self.period_id))
            db.commit()
            return {'revision':newrev,'settings':value,'readiness':readiness_report(json.loads(row['source_json']),value),'saved':True}

    def _assert_client_version(self, row, payload):
        if not isinstance(payload,dict) or type(payload.get('revision')) is not int or not isinstance(payload.get('sourceHash'),str):
            raise ValueError('درخواست باید نسخه تنظیمات و شناسه داده Excel را همراه داشته باشد.')
        if payload['revision'] != row['revision'] or payload['sourceHash'] != row['source_hash']:
            raise Conflict('اطلاعات داشبورد از زمان آخرین بررسی تغییر کرده‌اند. ابتدا صفحه را تازه‌سازی و نتیجه را دوباره بررسی کنید.')

    def freeze(self, payload):
        # Refresh and then validate/freeze the exact accepted DB snapshot under
        # the same lock. No settings write or Excel apply may interleave here.
        with LOCK:
            status=self.refresh()
            with db_connection() as db:
                db.execute('BEGIN IMMEDIATE')
                row=db_row(db,self.period_id)
                self._assert_client_version(row,payload)
                error=readiness_error(json.loads(row['source_json']),json.loads(row['settings_json']))
                if error:
                    raise ValueError('امکان شروع مراسم وجود ندارد: '+error)
                if not row['frozen']:
                    db.execute('UPDATE evaluations SET frozen=1,updated_at=? WHERE period_id=?',
                               (now_iso(),self.period_id))
                    row=db_row(db,self.period_id)
                db.commit()
                return self._state_from_row(row,status)

    def apply(self, payload):
        # An explicit action, never an automatic background refresh.
        with LOCK,db_connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db_row(db,self.period_id)
            self._assert_client_version(row,payload)
            if row['pending_json'] is not None:
                db.execute('''UPDATE evaluations SET source_json=pending_json,source_hash=pending_hash,
                              pending_json=NULL,pending_hash=NULL,frozen=0,updated_at=? WHERE period_id=?''',(now_iso(),self.period_id))
            else:
                db.execute('UPDATE evaluations SET frozen=0 WHERE period_id=?',(self.period_id,))
            db.commit()
        return self.state()
    def backup(self):
        # Consistent SQLite backup via sqlite3 API rather than copying an open DB file.
        with LOCK:
            fd,name=tempfile.mkstemp(suffix='.sqlite3')
            os.close(fd)
            try:
                with db_connection() as src, db_connection(name) as dest:
                    src.backup(dest)
                raw=Path(name).read_bytes()
            finally:
                os.unlink(name)
        return raw


class Conflict(Exception):
    pass


def _safe_inline_json(value):
    # Prevent workbook-supplied text from terminating the inline script.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')


def render_application_html(data):
    template=(BASE_DIR/'index.template.html').read_text(encoding='utf-8')
    tokens={
        '__DATA_JSON__': _safe_inline_json(data),
        '__HISTORICAL_RECORDS_JSON__': _safe_inline_json(PRIVATE_CONFIG.get('historical_supporters', [])),
        '__BOOK_EXCLUSION_JSON__': _safe_inline_json(PRIVATE_CONFIG.get('top_book_exclusion', {})),
        '__SUPPORTER_EXCLUSION_JSON__': _safe_inline_json(next(iter(SUPPORTER_EXCLUDED), None)),
    }
    for key,value in tokens.items():
        if template.count(key)!=1:
            raise ValueError('Missing or duplicate presentation token: '+key)
        template=template.replace(key,value)
    return template


class Handler(BaseHTTPRequestHandler):
    dashboard=None
    def local_request_ok(self, mutation=False):
        # Lightweight local-only HTTP boundary: no login, no account infrastructure.
        port=self.server.server_port
        expected={'127.0.0.1:'+str(port), 'localhost:'+str(port)}
        supplied=(self.headers.get('Host') or '').strip().lower()
        if supplied not in expected:
            self.as_json({'error':'نام میزبان مجاز نیست؛ از http://127.0.0.1:'+str(port)+'/ استفاده کنید.'},403)
            return False
        if mutation:
            origin=self.headers.get('Origin')
            if origin and origin.lower() not in {'http://'+h for h in expected}:
                self.as_json({'error':'درخواست از مبدأ نامعتبر ارسال شده است.'},403)
                return False
            content_type=(self.headers.get('Content-Type') or '').split(';',1)[0].strip().lower()
            if content_type!='application/json':
                self.as_json({'error':'برای ثبت تغییرات فقط Content-Type: application/json مجاز است.'},415)
                return False
        return True
    def log_message(self,fmt,*args):
        if not self.path.startswith('/api/state'): super().log_message(fmt,*args)

    def respond(self,body,content_type='application/json; charset=utf-8',status=200,download_name=None):
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Cache-Control','no-cache, no-store, must-revalidate')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Length',str(len(body)))
        if download_name:
            self.send_header('Content-Disposition','attachment; filename="'+download_name+'"')
        self.end_headers()
        self.wfile.write(body)

    def as_json(self,obj,status=200):
        return self.respond(compact(obj).encode('utf-8'),status=status)

    def do_GET(self):
        if not self.local_request_ok(): return
        route=urlsplit(self.path).path
        try:
            if route in ('/','/index.html'):
                return self.respond(render_application_html(self.dashboard.state()['data']).encode('utf-8'),'text/html; charset=utf-8')
            if route in ('/api/state','/api/data'):
                return self.as_json(self.dashboard.state())
            if route=='/api/supporters':
                if (self.dashboard.filename, self.dashboard.sheet) != (EXCEL_FILE, SHEET_NAME):
                    return self.as_json({'error':'اطلاعات پشتیبانان فقط متعلق به دوره تأییدشدهٔ فعلی است.'},409)
                params=parse_qs(urlsplit(self.path).query,keep_blank_values=True)
                managers=tuple(SUPPORTER_MANAGER_ALIAS.get(sheet,sheet) for sheet in SUPPORTER_SHEETS if sheet not in SUPPORTER_EXCLUDED)
                if (len(params.get('manager',[]))!=1 or len(params.get('metric',[]))!=1 or
                    params['manager'][0] not in managers or
                    params['metric'][0] not in {m for m,_ in SUPPORTER_METRICS}):
                    return self.as_json({'error':'مدیر یا معیار در محدودهٔ مجاز نیست.'},422)
                manager,metric=params['manager'][0],params['metric'][0]
                source=read_supporters()
                scoped=[{'manager':r['manager'],'managerSheet':r['managerSheet'],
                         'name':r['name'],'row':r['row'], 'value':r['values'][metric]}
                        for r in source['records'] if r['manager']==manager]
                issues=[r for r in source['issues'] if r['manager']==manager] if metric=='فیلم پیشرفت' else []
                return self.as_json({'sourceSha256':source['sourceSha256'], 'manager':manager,
                                     'metric':metric, 'records':scoped, 'issues':issues})
            if route=='/api/backup':
                return self.respond(self.dashboard.backup(),'application/octet-stream',download_name='Kanoon_Dashboard_Backup.sqlite3')
            if route=='/favicon.ico':return self.respond(b'','image/x-icon',204)
            return self.as_json({'error':'Not found'},404)
        except Exception as e:
            return self.as_json({'error':str(e)},503)

    def do_POST(self):
        route=urlsplit(self.path).path
        if route not in ('/api/settings','/api/freeze','/api/apply'):
            return self.as_json({'error':'Not found'},404)
        if not self.local_request_ok(mutation=True): return
        try:
            size=int(self.headers.get('Content-Length','-1'))
            if size<0 or size>MAX_BODY: return self.as_json({'error':'Invalid request size'},413)
            body=self.rfile.read(size)
            payload=json.loads(body.decode('utf-8'),parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Non-finite JSON number')))
            if route=='/api/settings':result=self.dashboard.save_settings(payload)
            elif route=='/api/freeze':result=self.dashboard.freeze(payload)
            else:result=self.dashboard.apply(payload)
            return self.as_json(result)
        except Conflict as exc:
            return self.as_json({'error':str(exc)},409)
        except (ValueError,TypeError,KeyError) as exc:
            return self.as_json({'error':str(exc)},422)
        except Exception as exc:
            return self.as_json({'error':str(exc)},503)


def occupied_port_hint():
    """Best-effort diagnostic; never attach to/stop an existing listener."""
    if os.name != 'nt':
        return 'برای بررسی فرایند فعال، پورت 8765 را با ابزارهای سیستم بررسی کنید.'
    try:
        result=subprocess.run(['netstat','-ano','-p','tcp'],capture_output=True,
                              text=True,errors='replace',timeout=3,check=False)
        for line in result.stdout.splitlines():
            fields=line.split()
            if len(fields)>=5 and fields[1].endswith(':8765') and fields[3].upper()=='LISTENING':
                return 'PID فرایند اشغال‌کننده: '+fields[4]+' (مسیر را در Task Manager بررسی کنید).'
    except (OSError,subprocess.TimeoutExpired):
        pass
    return 'برای مشاهده PID در Windows: netstat -ano | findstr :8765'


def main():
    parser=argparse.ArgumentParser(description='Kanoon Manager Dashboard — offline, SQLite-backed')
    parser.add_argument('workbook',nargs='?',default=str(DATA_DIR/EXCEL_FILE))
    parser.add_argument('--sheet',default=SHEET_NAME,help='Exact Excel sheet name, e.g. 3 آذر')
    args=parser.parse_args()
    path=Path(args.workbook).expanduser().resolve()
    # Bind BEFORE opening SQLite / refreshing Excel. A second launched copy
    # must not modify data or open the browser of a different, older listener.
    try:
        server=ThreadingHTTPServer((HOST,PORT),Handler)
    except OSError as exc:
        print('ERROR: پورت 8765 اشغال است؛ نسخه جدید اجرا نشد و مرورگر باز نخواهد شد.',file=sys.stderr)
        print('Existing listener:',occupied_port_hint(),file=sys.stderr)
        print('نسخه قبلی را در CMD/Task Manager به صورت دستی ببندید و سپس دوباره اجرا کنید.',file=sys.stderr)
        print('Bind failure:',exc,file=sys.stderr)
        return 3
    try:
        dashboard=Dashboard(path,args.sheet)
        Handler.dashboard=dashboard
    except Exception as exc:
        server.server_close()
        print('Cannot initialize evaluation:',exc,file=sys.stderr)
        return 2
    addr='http://%s:%s/'%(HOST,PORT)
    print('Kanoon Manager Dashboard v1.6.0 TOP SUPPORTERS (history active, book cutoff 60)')
    print('Open:',addr)
    print('Excel input:',path)
    print('Period:',args.sheet)
    print('SQLite data:',DB_PATH)
    print('Excel is read-only. SQLite stores the accepted snapshot and custom settings.')
    print('Offline operation works after first successful Excel import.')
    if os.environ.get('LOCAL_OPS_NO_BROWSER') != '1':
        Timer(.6,lambda:webbrowser.open(addr)).start()
    try: server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt: print('\nStopped')
    finally: server.server_close()
    return 0

if __name__=='__main__':raise SystemExit(main())
