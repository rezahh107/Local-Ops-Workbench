"""Required real Chromium browser verification; fails rather than silently skipping."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from synthetic_fixtures import MANAGERS,SHEETS,source_workbook,supporters_workbook



def assert_authoritative_scores(values):
    # Independent weighted-average oracle from the synthetic fixture, NOT from calculate().
    scores={entry['name']:entry['score'] for entry in values}
    expected={
        MANAGERS[0]:(100*85+70*90+40*100)/210,
        MANAGERS[1]:(100*80+40*50)/140,
        MANAGERS[2]:(100*70+40*75)/140,
    }
    for manager,correct in expected.items():
        assert manager in scores and scores[manager] is not None
        assert abs(scores[manager]-correct)<.0001, 'ACTUAL_SCORING_MISMATCH'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--loopback-proxy',action='store_true',help='Browser UI test via synthetic Python HTTP broker; not native browser networking')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='localops-browser-synthetic-') as t:
        d=Path(t)
        source_workbook(d/'آبان 1405(3).xlsx')
        supporter=supporters_workbook(d/'Shiraz_Supporters_With_Video_View_Percentage.xlsx')
        config={
            'intelligence_eligible':[MANAGERS[0]],
            'supporters':{'sha256':hashlib.sha256(supporter.read_bytes()).hexdigest(),
                          'manager_alias':{},'excluded':[SHEETS[-1]],'sheets':SHEETS},
            'top_book_exclusion':{'manager':SHEETS[0],'name':'نمونه ۱-۱'},
            'historical_supporters':[{'name':'داوطلب آزمایشی','score':93}],
        }
        (d/'config.private.json').write_text(json.dumps(config,ensure_ascii=False),encoding='utf8')
        os.environ['LOCAL_OPS_DATA_DIR']=str(d)
        import server
        server.DB_PATH=d/'dashboard.sqlite3'
        app=server.Dashboard(d/'آبان 1405(3).xlsx','آبان 1405')
        class Handler(server.Handler):pass
        Handler.dashboard=app
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{http.server_port}'
        try:
            with sync_playwright() as p:
                options={'headless':True}
                if os.getenv('LOCAL_OPS_BROWSER_EXECUTABLE'):
                    options['executable_path']=os.environ['LOCAL_OPS_BROWSER_EXECUTABLE']
                browser=p.chromium.launch(**options)
                page=browser.new_page(viewport={'width':1280,'height':720},locale='fa-IR')
                try:
                    if args.loopback_proxy:
                        # Renderer-only local testing: browser's localhost networking is
                        # blocked in this host, so fetch is bridged to the genuine
                        # synthetic Python server. Not equivalent to native HTTP.
                        reject_next={'enabled':False}
                        def bridge(path,method='GET',body='',headers=None):
                            if reject_next['enabled'] and path=='/api/settings' and method=='POST':
                                reject_next['enabled']=False
                                return {'status':409,'text':json.dumps({'error':'Synthetic competing revision'})}
                            if not isinstance(path,str): return {'status':599,'text':'{}'}
                            upstream=Request(base+path,data=body.encode('utf-8') if method!='GET' else None,
                                             method=method,headers=headers or {})
                            try:
                                with urlopen(upstream,timeout=10) as response:
                                    status,data=response.status,response.read()
                            except HTTPError as err:
                                status,data=err.code,err.read()
                            return {'status':status,'text':data.decode('utf-8')}
                        page.expose_function('__bridge',bridge)
                        page.evaluate("""window.fetch=async (path,options={})=>{
                          const r=await window.__bridge(path,options.method||'GET',options.body||'',options.headers||{});
                          return {ok:r.status>=200&&r.status<300,status:r.status,
                            json:async()=>JSON.parse(r.text),text:async()=>r.text};
                        };""")
                    errors=[]
                    page.on('pageerror',lambda err:errors.append(str(err)))
                    if args.loopback_proxy:
                        page.set_content(server.render_application_html(app.state()['data']),wait_until='domcontentloaded',timeout=10000)
                    else:
                        page.goto(base+'/',wait_until='domcontentloaded',timeout=10000)
                    page.wait_for_function('typeof serverReady!=="undefined" && serverReady && connectionHealthy',timeout=10000)
                    assert not errors,errors
                    # The actual browser scoring output, not a self-confirming Python fixture.
                    values=page.evaluate('calculate().map(r=>({name:r.name,score:r.score}))')
                    assert_authoritative_scores(values)
                    # Mutation check: deliberately corrupt the *actual browser function*
                    # then demand the same independent oracle reject the wrong result.
                    page.evaluate('window.__baselineCalculate=calculate; calculate=function(){return __baselineCalculate().map(r=>({...r,score:r.score===null?null:r.score/2}));}')
                    try:
                        corrupted=page.evaluate('calculate().map(r=>({name:r.name,score:r.score}))')
                        try:
                            assert_authoritative_scores(corrupted)
                        except AssertionError:
                            pass
                        else:
                            raise AssertionError('Scoring oracle accepted deliberately wrong browser calculation')
                    finally:
                        page.evaluate('calculate=window.__baselineCalculate; delete window.__baselineCalculate')
                    # The configured private exception keeps the existing book behavior.
                    cfg_result=page.evaluate("""()=>({configured:topBookConfigured(), excluded:topBookExcluded('کتاب',
                        {manager:'گروه آزمایشی 1',name:'نمونه ۱-۱'})})""")
                    assert cfg_result=={'configured':True,'excluded':True}
                    # Missing private config must never claim the book shortlist is safe.
                    original_config=server.PRIVATE_CONFIG
                    try:
                        server.PRIVATE_CONFIG={}
                        no_config_html=server.render_application_html(app.state()['data'])
                    finally:
                        server.PRIVATE_CONFIG=original_config
                    missing_page=browser.new_page()
                    try:
                        missing_page.set_content(no_config_html,wait_until='domcontentloaded')
                        missing=missing_page.evaluate("""()=>({configured:topBookConfigured(),
                           featured:topIsFeatured('کتاب',{manager:'گروه آزمایشی 1',name:'نمونه ۱-۱',value:{status:'scored',effective:95}}),
                           other:topIsFeatured('درصد حضور کل',{manager:'گروه آزمایشی 1',name:'نمونه ۱-۱',value:{status:'scored',effective:95}})})""")
                        assert missing=={'configured':False,'featured':False,'other':True}
                    finally:
                        missing_page.close()
                    page.locator('[data-view="metrics"]').click()
                    page.locator('#editCustomMetric').select_option('xlsname:درصد حضور کل')
                    page.locator('#entryCompact').click()
                    assert page.locator('.entry-row').count()==len(MANAGERS)
                    cell=page.locator('[data-entry-index="0"] [data-cell-value]')
                    assert float(cell.input_value())==85
                    cell.fill('92');cell.press('Tab')
                    page.wait_for_function('showSaved && savesPending===0 && !saveError',timeout=12000)
                    saved=app.state()['settings']['cells'][server.metric_id('درصد حضور کل')][MANAGERS[0]]
                    assert saved['value']==92
                    if args.loopback_proxy:
                        page.close()
                        page=browser.new_page(viewport={'width':1280,'height':720},locale='fa-IR')
                        page.expose_function('__bridge',bridge)
                        page.evaluate("""window.fetch=async (path,options={})=>{
                          const r=await window.__bridge(path,options.method||'GET',options.body||'',options.headers||{});
                          return {ok:r.status>=200&&r.status<300,status:r.status,
                            json:async()=>JSON.parse(r.text),text:async()=>r.text};
                        };""")
                        page.set_content(server.render_application_html(app.state()['data']),wait_until='domcontentloaded',timeout=10000)
                        page.wait_for_function('serverReady && connectionHealthy',timeout=10000)
                    else:page.reload()
                    page.locator('[data-view="metrics"]').click()
                    page.locator('#editCustomMetric').select_option('xlsname:درصد حضور کل')
                    page.locator('#entryCompact').click()
                    assert float(page.locator('[data-entry-index="0"] [data-cell-value]').input_value())==92
                    page.locator('[data-entry-index="0"] [data-restore-cell]').click()
                    page.wait_for_function('showSaved && savesPending===0 && !saveError',timeout=12000)
                    assert float(page.locator('[data-entry-index="0"] [data-cell-value]').input_value())==85
                    # Presentation is actually visible, not merely a CSS class in DOM.
                    page.locator('[data-view="overview"]').click()
                    page.locator('#presentBtn').click()
                    assert page.locator('#projectorStage').is_visible()
                    page.locator('#projectExit').click()
                    assert page.locator('#projectorStage').is_hidden()
                    # A backend conflict must remain visible and never be reported
                    # as a successful persisted browser edit.
                    revision_before=app.state()['revision']
                    if args.loopback_proxy:reject_next['enabled']=True
                    else:
                        page.route(base+'/api/settings',lambda route: route.fulfill(status=409,
                                   content_type='application/json',body='{"error":"Synthetic competing revision"}'),times=1)
                    page.locator('[data-view="metrics"]').click()
                    page.locator('#editCustomMetric').select_option('xlsname:درصد حضور کل')
                    page.locator('#entryCompact').click()
                    blocked=page.locator('[data-entry-index="0"] [data-cell-value]')
                    blocked.fill('93');blocked.press('Tab')
                    page.wait_for_function('!!saveError && savesPending===0 && !showSaved',timeout=12000)
                    assert page.locator('#saveBanner').is_visible()
                    assert 'ثبت' in page.locator('#saveBanner').inner_text() or 'تعارض' in page.locator('#saveBanner').inner_text()
                    assert app.state()['revision']==revision_before
                    print('BROWSER_CONTRACT_PASS: authoritative scoring + scoring-mutation rejection / private book config / compact persistence / restore / presentation / rejected-write feedback | transport='+('PYTHON_LOOPBACK_BROKER' if args.loopback_proxy else 'NATIVE_BROWSER'),flush=True)
                finally:browser.close()
        finally:
            http.shutdown();http.server_close();thread.join(timeout=3)


if __name__=='__main__':main()
