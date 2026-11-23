"""Local-only, explicit non-overwriting extraction of private CONFIG (not data).

Do not upload the original ZIP or generated private configuration to Git/CI.
"""
import argparse
import ast
import hashlib
import json
import re
from pathlib import Path
from zipfile import ZipFile

EXPECTED_SHA256 = '3f092528ee612827eb83734e32964f2a025cf99b0179e6db08b0df27b6fc8e51'


def from_baseline(archive):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != EXPECTED_SHA256:
        raise ValueError('Original baseline ZIP identity mismatch: refusing private extraction')
    with ZipFile(archive) as z:
        source = z.read('server.py').decode('utf-8')
        html = z.read('index.template.html').decode('utf-8')
    constants = {}
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id in {
                    'INTELLIGENCE_ELIGIBLE', 'SUPPORTERS_SHA256',
                    'SUPPORTER_MANAGER_ALIAS', 'SUPPORTER_EXCLUDED', 'SUPPORTER_SHEETS',
                }:
                    constants[target.id] = ast.literal_eval(statement.value)
    required = {'INTELLIGENCE_ELIGIBLE', 'SUPPORTERS_SHA256',
                'SUPPORTER_MANAGER_ALIAS', 'SUPPORTER_EXCLUDED', 'SUPPORTER_SHEETS'}
    if set(constants) != required:
        raise ValueError('Baseline layout differs: private config extraction was not qualified')
    block = re.search(r'const TOP_HISTORICAL_RECORDS=Object\.freeze\(\[(.*?)\]\.map', html, re.S)
    if not block:
        raise ValueError('Historical data block missing')
    entries = re.findall(r"\{name:'((?:[^'\\]|\\.)*)',score:([0-9]+(?:\.[0-9]+)?)\}",block.group(1))
    if len(entries) != 7:
        raise ValueError('Historical record count differs from approved baseline')
    if any('\\' in name for name, _ in entries):
        raise ValueError('Escaped JS names require manual review')
    exclusion = re.search(r"function topBookExcluded\(metric,record\)\{return metric==='کتاب'&&record\.manager==='([^']+)'&&record\.name==='([^']+)';\}",html)
    if not exclusion:
        raise ValueError('Book exclusion not identified')
    return {
        'intelligence_eligible': list(constants['INTELLIGENCE_ELIGIBLE']),
        'supporters': {
            'sha256': constants['SUPPORTERS_SHA256'],
            'manager_alias': constants['SUPPORTER_MANAGER_ALIAS'],
            'excluded': sorted(constants['SUPPORTER_EXCLUDED']),
            'sheets': list(constants['SUPPORTER_SHEETS']),
        },
        'top_book_exclusion': {'manager':exclusion.group(1),'name':exclusion.group(2)},
        'historical_supporters': [{'name':n,'score':float(score)} for n,score in entries],
    }


def main():
    ap=argparse.ArgumentParser(description='Prepare private config locally from exact original ZIP; never upload output')
    ap.add_argument('original_zip',type=Path)
    ap.add_argument('--data-dir',type=Path,required=True)
    ap.add_argument('--write',action='store_true',help='Explicitly create new private config; never overwrite')
    args=ap.parse_args()
    cfg=from_baseline(args.original_zip)
    destination=(args.data_dir.expanduser().resolve()/'config.private.json')
    if destination.exists():
        raise SystemExit('REFUSED: target private configuration already exists')
    if not args.write:
        print('DRY RUN: exact source verified; private config can be created in the requested external data directory')
        return
    destination.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive creation prevents TOCTOU replacement and avoids printing private details.
    fd=destination.open('x',encoding='utf-8')
    try:
        fd.write(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n')
    finally:
        fd.close()
    try:destination.chmod(0o600)
    except OSError:pass
    print('CREATED private config outside source tree (not in VCS). No Excel/SQLite files modified.')


if __name__=='__main__':main()
