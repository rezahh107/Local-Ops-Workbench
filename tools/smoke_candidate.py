"""Real subprocess HTTP/SQLite smoke from clean source extraction and synthetic upgrade."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

from package_candidate import extract_source,validate


def find_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0))
        return s.getsockname()[1]


def get_state(base):
    with urlopen(base+'/api/state',timeout=2) as response:
        return json.load(response)


@contextmanager
def running_app(root, data):
    port=find_port()
    env={**os.environ,'LOCAL_OPS_DATA_DIR':str(data),'LOCAL_OPS_PORT':str(port),
         'LOCAL_OPS_NO_BROWSER':'1','PYTHONUNBUFFERED':'1'}
    proc=subprocess.Popen([sys.executable,'-u',str(root/'server.py')],cwd=root,env=env,
                          stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    base=f'http://127.0.0.1:{port}'
    try:
        for _ in range(60):
            if proc.poll() is not None:
                raise AssertionError('Packaged application exited before smoke check')
            try:
                state=get_state(base)
                break
            except (OSError,ValueError):
                time.sleep(.1)
        else:
            raise AssertionError('Packaged application HTTP startup timed out')
        yield base,state
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill();proc.communicate()


def smoke(path):
    path=Path(path)
    validate(path)
    with tempfile.TemporaryDirectory(prefix='localops-extracted-') as temp:
        temp=Path(temp)
        code1=temp/'source-v1';code2=temp/'source-v2'
        data=temp/'private-operational-data';data.mkdir()
        extract_source(path,code1)
        sys.path.insert(0,str(code1/'tests'))
        try:
            from synthetic_fixtures import MANAGERS,SHEETS,source_workbook,supporters_workbook
        finally:
            sys.path.pop(0)
        excel=source_workbook(data/'آبان 1405(3).xlsx')
        supporters=supporters_workbook(data/'Shiraz_Supporters_With_Video_View_Percentage.xlsx')
        original_excel=hashlib.sha256(excel.read_bytes()).hexdigest()
        original_supporters=hashlib.sha256(supporters.read_bytes()).hexdigest()
        config={
            'intelligence_eligible':[MANAGERS[0]],
            'supporters':{'sha256':original_supporters,
                          'manager_alias':{},'excluded':[SHEETS[-1]],'sheets':SHEETS},
            'top_book_exclusion':{'manager':SHEETS[0],'name':'نمونه ۱-۱'},
            'historical_supporters':[{'name':'داوطلب آزمایشی','score':93}],
        }
        cfg_path=data/'config.private.json'
        cfg_path.write_text(json.dumps(config,ensure_ascii=False),encoding='utf8')
        cfg_digest=hashlib.sha256(cfg_path.read_bytes()).hexdigest()
        with running_app(code1,data) as (base,state):
            if state['data']['managers']!=MANAGERS or not state['readiness']['ready'] or state['revision']!=0:
                raise AssertionError('First-run synthetic state mismatch')
            with urlopen(base+'/',timeout=2) as response:
                html=response.read().decode('utf8')
            if not ('<html' in html and 'داوطلب آزمایشی' in html and '__DATA_JSON__' not in html):
                raise AssertionError('Real HTML presentation missing synthetic data')
            settings=state['settings']
            settings['cells']['xlsname:درصد حضور کل']={MANAGERS[0]:{'status':'scored','value':92}}
            request=Request(base+'/api/settings',method='POST',
                            data=json.dumps({'revision':state['revision'],'settings':settings},ensure_ascii=False).encode('utf8'),
                            headers={'Content-Type':'application/json'})
            with urlopen(request,timeout=3) as response:
                if response.status!=200:raise AssertionError('Synthetic state save failed')
            saved=get_state(base)
            if saved['revision']<=0 or saved['settings']['cells']['xlsname:درصد حضور کل'][MANAGERS[0]]['value']!=92:
                raise AssertionError('Synthetic SQLite save was not persistent')
        database=data/'dashboard.sqlite3'
        if not database.is_file() or (code1/'dashboard.sqlite3').exists():
            raise AssertionError('SQLite must be stored outside source tree')
        persisted_revision=saved['revision']
        # Upgrade is a new source directory, not extraction over existing code/data.
        extract_source(path,code2)
        try:
            extract_source(path,data)
        except ValueError as exc:
            if 'Refusing to replace' not in str(exc):raise
        else:
            raise AssertionError('An upgrade could overwrite pre-existing operational data')
        with running_app(code2,data) as (base,after):
            if after['revision']!=persisted_revision:
                raise AssertionError('Upgrade replaced or reset SQLite revision')
            cell=after['settings']['cells']['xlsname:درصد حضور کل'][MANAGERS[0]]
            if cell['value']!=92:
                raise AssertionError('Upgrade lost synthetic working data')
            if after['data']['managers']!=MANAGERS:
                raise AssertionError('Upgrade lost source workbook data')
        if hashlib.sha256(excel.read_bytes()).hexdigest()!=original_excel:
            raise AssertionError('Source Excel was modified by installation/upgrade')
        if hashlib.sha256(supporters.read_bytes()).hexdigest()!=original_supporters:
            raise AssertionError('Supporters Excel was modified by installation/upgrade')
        if hashlib.sha256(cfg_path.read_bytes()).hexdigest()!=cfg_digest:
            raise AssertionError('Private configuration was overwritten')
        if (code2/'dashboard.sqlite3').exists() or (code2/'META/manifest.json').exists():
            raise AssertionError('Operational data or package metadata leaked into extracted source')
        return {'clean_extraction':'PASS','upgrade_without_overwrite':'PASS',
                'startup':'PASS','actual_http':'PASS','external_sqlite_persistence':'PASS',
                'read_only_excel':'PASS','source_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

if __name__=='__main__':
    if len(sys.argv)!=2:raise SystemExit('Usage: smoke_candidate.py validated-candidate.zip')
    try:
        print(json.dumps(smoke(Path(sys.argv[1])),ensure_ascii=False))
    except Exception as exc:
        print('SMOKE_FAILED:',type(exc).__name__,str(exc),file=sys.stderr)
        raise SystemExit(1)
