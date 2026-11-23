"""Pure-standard-library XLSX fixtures with fabricated identities only."""
from xml.sax.saxutils import escape, quoteattr
from zipfile import ZipFile, ZIP_DEFLATED

MANAGERS = ['آزمایشی الف', 'آزمایشی ب', 'آزمایشی پ']
SHEETS = [f'گروه آزمایشی {i}' for i in range(1, 11)]
INTELLIGENCE = [MANAGERS[0]]
METRICS = [
    ['درصد حضور کل', 100, 85, 80, 70],
    ['کلاس هوش', 70, 90, None, None],
    ['کتاب', 40, 100, 50, 75],
]


def _cell(value, column, row):
    from string import ascii_uppercase
    addr=ascii_uppercase[column]+str(row)
    if value is None: return ''
    if isinstance(value,(int,float)):
        return f'<c r="{addr}"><v>{value}</v></c>'
    return f'<c r="{addr}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'


def workbook(path, sheets):
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    src=[]; relations=[]
    with ZipFile(path,'w',ZIP_DEFLATED) as z:
        for i,(name,rows) in enumerate(sheets.items(),start=1):
            src.append(f'<sheet name={quoteattr(name)} sheetId="{i}" r:id="rId{i}"/>')
            relations.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
            body=''.join(f'<row r="{j}">'+''.join(_cell(v,k,j) for k,v in enumerate(row))+'</row>' for j,row in enumerate(rows,start=1))
            z.writestr(f'xl/worksheets/sheet{i}.xml',f'<worksheet xmlns="{ns}"><sheetData>{body}</sheetData></worksheet>')
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>{"".join(src)}</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(relations)+'</Relationships>')
    return path


def source_workbook(path, *,first_score=85):
    metrics=[list(row) for row in METRICS]
    metrics[0][2]=first_score
    return workbook(path,{'آبان 1405': [['عملکرد','ضرائب',*MANAGERS],*metrics]})


def supporters_workbook(path):
    header=['ردیف','نام مدیر','نام پشتیبان','میانگین درصد حضوری','میانگین درصد کل','درصد فروش کتاب','درصد مشاهده فیلم']
    result={}
    counter=0
    for idx,sheet in enumerate(SHEETS):
        # Ten groups, nine visible after excluding the two-row final group.
        count=2 if idx==9 else (7 if idx<7 else 6)
        rows=[header]
        for j in range(count):
            counter+=1
            rows.append([j+1,sheet,f'نمونه {idx+1}-{j+1}',0.8,0.9,0.7,None if counter<=16 else 0.5])
        result[sheet]=rows
    assert counter==63
    result['کنترل مشاهده فیلم']=[['مدیر','پشتیبان','شرح','-','منبع','-','-','-','-','توضیح'],
        [SHEETS[0],'نمونه ۱-۱','دو مقدار متفاوت برای یک فرد',None,'منبع آزمایشی',None,None,None,None,'اختلاف آزمایشی'],
        [SHEETS[1],'نمونه ۲-۱','دو مقدار متفاوت برای یک فرد',None,'منبع آزمایشی',None,None,None,None,'اختلاف آزمایشی']]
    return workbook(path,result)
