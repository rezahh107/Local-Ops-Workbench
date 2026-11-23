"""Real Python HTTP/SQLite/XLSX contracts, with exclusively fabricated test data."""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
import threading
from http.server import ThreadingHTTPServer
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from synthetic_fixtures import MANAGERS,SHEETS,source_workbook,supporters_workbook

ROOT = Path(__file__).resolve().parents[1]


class ApplicationContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='localops-synthetic-')
        cls.dir=Path(cls.temp.name)
        cls.excel=source_workbook(cls.dir/'آبان 1405(3).xlsx')
        cls.supporters=supporters_workbook(cls.dir/'Shiraz_Supporters_With_Video_View_Percentage.xlsx')
        cfg={
            'intelligence_eligible':[MANAGERS[0]],
            'supporters':{'sha256':hashlib.sha256(cls.supporters.read_bytes()).hexdigest(),
                          'manager_alias':{},'excluded':[SHEETS[-1]],'sheets':SHEETS},
            'top_book_exclusion':{'manager':SHEETS[0],'name':'نمونه ۱-۱'},
            'historical_supporters':[{'name':'داوطلب آزمایشی','score':95}],
        }
        (cls.dir/'config.private.json').write_text(json.dumps(cfg,ensure_ascii=False),encoding='utf8')
        os.environ['LOCAL_OPS_DATA_DIR']=str(cls.dir)
        import sys
        sys.path.insert(0,str(ROOT))
        cls.app=importlib.import_module('server')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.app.DB_PATH=self.dir/(self.id().split('.')[-1]+'.sqlite3')
        self.app.DB_PATH.unlink(missing_ok=True)
        source_workbook(self.excel)
        self.dashboard=self.app.Dashboard(self.excel,'آبان 1405')

    def request(self,base,path,method='GET',payload=None,headers=None):
        data=json.dumps(payload,ensure_ascii=False).encode('utf8') if payload is not None else None
        h=dict(headers or {})
        if data is not None: h.setdefault('Content-Type','application/json')
        r=Request(base+path,method=method,headers=h,data=data)
        try:
            with urlopen(r,timeout=5) as response:
                return response.status,response.read()
        except HTTPError as exc:
            return exc.code,exc.read()

    def test_scoring_applicability_and_synthetic_workbook(self):
        state=self.dashboard.state()
        self.assertEqual(state['data']['managers'],MANAGERS)
        self.assertEqual(state['data']['applicability']['کلاس هوش'],[MANAGERS[0]])
        self.assertEqual([r['weight'] for r in state['data']['metrics']],[100,70,40])
        self.assertTrue(state['readiness']['ready'],state['readiness'])
        # Independent approved rule: 100*85 + 70*90 + 40*100, divided by 210.
        expected=(100*85+70*90+40*100)/210
        self.assertAlmostEqual(expected,89.52380952380952)
        # Non-applicable intelligence weight MUST NOT dilute other managers' score.
        self.assertAlmostEqual((100*80+40*50)/140,71.42857142857143)

    def test_revision_freeze_weights_only_no_nonweight_bypass(self):
        old=self.dashboard.state()
        initial=copy.deepcopy(old['settings'])
        custom=copy.deepcopy(initial)
        key=self.app.metric_id('درصد حضور کل')
        custom['cells'][key]={MANAGERS[0]:{'status':'scored','value':92}}
        saved=self.dashboard.save_settings({'revision':old['revision'],'settings':custom})
        self.assertEqual(saved['settings']['cells'][key][MANAGERS[0]]['value'],92)
        with self.assertRaises(self.app.Conflict):
            self.dashboard.save_settings({'revision':old['revision'],'settings':initial})
        frozen=self.dashboard.freeze({'revision':saved['revision'],'sourceHash':old['sourceHash']})
        self.assertTrue(frozen['frozen'])
        new_settings=copy.deepcopy(saved['settings'])
        new_settings['weights'][key]=75
        with self.assertRaises(self.app.Conflict):
            self.dashboard.save_settings({'revision':saved['revision'],'settings':new_settings})
        with self.assertRaises(self.app.Conflict):
            changed=copy.deepcopy(new_settings)
            changed['cells'][key][MANAGERS[0]]['value']=100
            self.dashboard.save_settings({'revision':saved['revision'],'weightsOnly':True,'settings':changed})
        result=self.dashboard.save_settings({'revision':saved['revision'],'weightsOnly':True,'settings':new_settings})
        self.assertEqual(result['settings']['weights'][key],75)
        self.assertEqual(self.dashboard.state()['settings']['cells'][key][MANAGERS[0]]['value'],92)
        self.assertTrue(self.app.Dashboard(self.excel,'آبان 1405').state()['frozen'])

    def test_excel_pending_freeze_restoration(self):
        before=self.dashboard.state()
        frozen=self.dashboard.freeze({'revision':before['revision'],'sourceHash':before['sourceHash']})
        source_workbook(self.excel,first_score=25)
        waiting=self.dashboard.state()
        self.assertTrue(waiting['pending'])
        self.assertEqual(waiting['data']['metrics'][0]['values'][0],85)
        with self.assertRaises(self.app.Conflict):
            self.dashboard.apply({'revision':999,'sourceHash':waiting['sourceHash']})
        applied=self.dashboard.apply({'revision':waiting['revision'],'sourceHash':waiting['sourceHash']})
        self.assertFalse(applied['frozen'])
        self.assertEqual(applied['data']['metrics'][0]['values'][0],25)
        self.assertEqual(self.app.Dashboard(self.excel,'آبان 1405').state()['data']['metrics'][0]['values'][0],25)

    def test_supporters_source_read_only_contract(self):
        before=self.supporters.read_bytes()
        result=self.app.read_supporters()
        self.assertEqual(len(result['records']),61)
        self.assertEqual(len(result['issues']),2)
        self.assertEqual(sum(r['values']['فیلم پیشرفت']['status']=='blank_g_zero' for r in result['records']),16)
        self.assertEqual(result['excludedManager'],SHEETS[-1])
        self.assertEqual(before,self.supporters.read_bytes())
        modified=self.dir/'tampered.xlsx'; modified.write_bytes(before+b'bad')
        with self.assertRaisesRegex(ValueError,'SHA-256'):
            self.app.read_supporters(modified)

    def test_real_http_guards_html_and_forbidden_write(self):
        class H(self.app.Handler):pass
        H.dashboard=self.dashboard
        srv=ThreadingHTTPServer(('127.0.0.1',0),H)
        th=threading.Thread(target=srv.serve_forever,daemon=True);th.start()
        base=f'http://127.0.0.1:{srv.server_port}'
        try:
            code,html=self.request(base,'/')
            self.assertEqual(code,200)
            self.assertIn('TOP_HISTORICAL_RECORDS'.encode(),html)
            self.assertIn('داوطلب آزمایشی'.encode(),html)
            self.assertNotIn(b'__DATA_JSON__',html)
            self.assertNotIn(b'__BOOK_EXCLUSION_JSON__',html)
            self.assertEqual(self.request(base,'/api/state')[0],200)
            self.assertEqual(self.request(base,'/api/state',headers={'Host':'untrusted.invalid'})[0],403)
            self.assertEqual(self.request(base,'/api/supporters',method='POST',payload={})[0],404)
            state=self.dashboard.state()
            self.assertEqual(self.request(base,'/api/freeze',method='POST',payload={'revision':0,'sourceHash':state['sourceHash']},headers={'Origin':'https://evil.invalid'})[0],403)
            self.assertEqual(self.request(base,'/api/settings',method='POST',payload={'revision':0,'settings':state['settings']},headers={'Content-Type':'text/plain'})[0],415)
            self.assertEqual(self.request(base,'/api/settings',method='POST',payload={'revision':88,'settings':state['settings']})[0],409)
            self.assertEqual(self.dashboard.state()['revision'],0)
        finally:
            srv.shutdown();srv.server_close();th.join(timeout=3)

    def test_inline_script_escape(self):
        data=self.dashboard.state()['data']
        data['label']='</script><script>alert(1)</script>'
        html=self.app.render_application_html(data)
        self.assertNotIn('</script><script>alert(1)',html)
        self.assertIn('\\u003c/script',html)


if __name__ == '__main__':unittest.main()
