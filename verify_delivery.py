"""Independent offline browser checks of exact published delivery bytes."""
import json
from pathlib import Path
import sys
import hashlib
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'vendor'))
from backend.config import Config
from backend.tools import ToolGateway
from backend.teams import get_team_agents

OUT=ROOT/'runs/delivery-01'
proof=OUT/'independent-checks';proof.mkdir(exist_ok=True)
def gateway(team):
    cfg=Config(ROOT/'config-deepseek')
    cfg.data_dir=OUT/team/'runtime'
    return ToolGateway(cfg,get_team_agents(team)),json.loads((OUT/team/'report.json').read_text())['runId']
def check(team,owner,name,args):
    g,run=gateway(team)
    result=g.execute(owner,run,'code.test',args)
    (proof/(name+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    b=result.get('browser',{})
    print(name,'exitCode',result.get('exitCode'),'assertions',[(x.get('selector'),x.get('passed')) for x in b.get('assertions',[])],flush=True)
    return result

events=[json.loads(l) for l in (OUT/'software/events.jsonl').read_text().splitlines()]
passed=next(e for e in reversed(events) if e.get('caseId')=='software-2' and e.get('agent')=='release' and e.get('operation')=='code.test' and e.get('result',{}).get('exitCode')==0)
paths={x['path']:x['sha256'] for x in passed['result']['browser']['testedFiles']}
g,run=gateway('software')
for path,digest in paths.items():
    assert hashlib.sha256((g.workspace('release',run)/path).read_bytes()).hexdigest()==digest
args={'language':'browser','path':passed['args']['path'],'resources':passed['args']['resources'],'checks':[
    {'action':'select','selector':'#book-room','value':'C'},
    {'action':'fill','selector':'#book-date','value':'2030-01-15'},
    {'action':'fill','selector':'#book-start','value':'09:00'},
    {'action':'fill','selector':'#book-end','value':'10:00'},
    {'action':'fill','selector':'#book-booker','value':'Independent reviewer'},
    {'action':'fill','selector':'#book-title','value':'Range calculation check'},
    {'action':'click','selector':'#book-form button[type=submit]'},
    {'action':'fill','selector':'#an-from','value':'2030-01-01'},
    {'action':'fill','selector':'#an-to','value':'2030-01-31'},
    {'action':'click','selector':'#an-apply'},
    {'action':'assert_text','selector':'#an-chart-count-alt','text':'Room A: 0 bookings; Room B: 0 bookings; Room C: 1 booking.'},
    {'action':'assert_text','selector':'#an-chart-time-alt','text':'Room A: 0:00; Room B: 0:00; Room C: 1:00.'},
    {'action':'assert_text','selector':'#an-chart-days-alt','text':'2030-01-15: 1 booking.'},
    {'action':'assert_text','selector':'#an-cancel-summary','text':'Total cancelled in range: 0 of 1 bookings (0% cancellation rate).'},
    {'action':'fill','selector':'#an-from','value':'2030-02-01'},
    {'action':'fill','selector':'#an-to','value':'2030-02-28'},
    {'action':'click','selector':'#an-apply'},
    {'action':'assert_text','selector':'#an-empty','text':'No bookings in this range.'},
    {'action':'fill','selector':'#an-from','value':'2030-01-01'},
    {'action':'fill','selector':'#an-to','value':'2030-01-31'},
    {'action':'click','selector':'#an-apply'},
    {'action':'click','selector':'#list-all'},
    {'action':'respond_dialog','dialogType':'prompt','accept':True,'value':'Independent cancellation check'},
    {'action':'click','selector':'#bookings-body button[data-cancel-id]'},
    {'action':'click','selector':'#an-apply'},
    {'action':'assert_text','selector':'#an-cancel-summary','text':'Total cancelled in range: 1 of 1 bookings (100% cancellation rate).'},
    {'action':'assert_text','selector':'#an-chart-time-alt','text':'Room A: 0:00; Room B: 0:00; Room C: 0:00.'},
    {'action':'reload'},
    {'action':'fill','selector':'#an-from','value':'2030-01-01'},
    {'action':'fill','selector':'#an-to','value':'2030-01-31'},
    {'action':'click','selector':'#an-apply'},
    {'action':'assert_text','selector':'#an-cancel-summary','text':'Total cancelled in range: 1 of 1 bookings (100% cancellation rate).'}]}
a=check('software','release','software-2-date-range',args)

events=[json.loads(l) for l in (OUT/'fund/events.jsonl').read_text().splitlines()]
pub=next(e for e in reversed(events) if e.get('operation')=='publish' and 'marketing/semiconductor_outlook_2026.html' in e.get('args',{}).get('paths',[]) and e.get('result',{}).get('ok'))
receipt=next(r for r in pub['result']['receipts'] if r['receiver']=='risk' and r['path'].endswith('semiconductor_outlook_2026.html'))
g,run=gateway('fund')
source=g.workspace('risk',run)/receipt['path']
assert hashlib.sha256(source.read_bytes()).hexdigest()==receipt['sha256']
html=source.read_text()
import re
count=len(re.findall(r'<section\s+class="slide',html))
checks=[{'action':'assert_count','selector':'.slide','count':count},
    {'action':'assert_count','selector':'.slide.on','count':1},
    {'action':'assert_text','selector':'.slide.on h1','text':'Semiconductor Industry Outlook'},
    {'action':'assert_text','selector':'#n','text':f'1 / {count}','exact':True},
    {'action':'click','selector':'button[onclick="go(1)"]'},
    {'action':'assert_text','selector':'#n','text':f'2 / {count}','exact':True},
    {'action':'press','selector':'body','key':'ArrowRight'},
    {'action':'assert_text','selector':'#n','text':f'3 / {count}','exact':True},
    {'action':'press','selector':'body','key':'ArrowLeft'},
    {'action':'assert_text','selector':'#n','text':f'2 / {count}','exact':True},
    {'action':'click','selector':'button[onclick="go(-1)"]'},
    {'action':'assert_text','selector':'#n','text':f'1 / {count}','exact':True}]
checks += [{'action':'click','selector':'button[onclick="go(1)"]'} for _ in range(count)]
checks += [{'action':'assert_text','selector':'#n','text':f'{count} / {count}','exact':True},{'action':'assert_count','selector':'.slide.on','count':1}]
b=check('fund','risk','fund-2-slide-navigation',{'language':'browser','path':receipt['path'],'checks':checks})
if a.get('exitCode')!=0 or b.get('exitCode')!=0: sys.exit(1)
