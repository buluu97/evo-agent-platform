"""Local-only HTTP app. Agent code always uses the Docker tool gateway."""
import os
os.environ['OTEL_SDK_DISABLED']='true'
os.environ['CREWAI_TELEMETRY_DISABLED']='true'
import argparse,json,mimetypes,sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs,quote
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'vendor'))
from live.platform import ActionError, Platform
from live.suite import SuiteManager

def handler(platform,suite):
 class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send(self,status,body,ctype='application/json; charset=utf-8',headers=None):
        if isinstance(body,(dict,list)):body=json.dumps(body,ensure_ascii=False).encode()
        if isinstance(body,str):body=body.encode()
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for k,v in (headers or {}).items():self.send_header(k,v)
        self.end_headers();self.wfile.write(body)
    def guard(self,mutation=False):
        host=self.headers.get('Host','');port=self.server.server_port
        if host not in (f'localhost:{port}',f'127.0.0.1:{port}'):
            raise PermissionError('Only the local application host is accepted')
        origin=self.headers.get('Origin')
        if origin and origin!=f'http://{host}':raise PermissionError('Same-origin access required')
        if mutation and self.headers.get('X-Crew-Client')!='1':raise PermissionError('Application client header required')
    def do_GET(self):
        try:
            self.guard();u=urlsplit(self.path);q={k:v[0] for k,v in parse_qs(u.query).items()};team=q.get('team','software');channel=q.get('channel','group')
            if u.path=='/api/state':
                with platform.lock:
                    if team not in platform.teams:
                        return self.send(200,{'nextTeam':next(iter(platform.teams),None)})
                    return self.send(200,platform.snapshot(team,channel,q.get('run')))
            if u.path=='/api/suite':return self.send(200,suite.state or {})
            if u.path=='/api/samples':return self.send(200,[{'team':g['team'],'roles':g['roles'],'tasks':g['tasks']} for g in __import__('benchmarks.mid_test_live',fromlist=['load_suite']).load_suite(suite.source)])
            if u.path=='/api/health':return self.send(200,{'provider':platform.config.provider,'model':platform.config.model,'configured':bool(platform.config.api_key),'serpapiConfigured':bool(platform.config.serpapi_api_key),'engine':'EvoAgent','runtimeVersion':platform.runtime_version,'thinkingPolicy':'review/repeated tool errors: low; ordinary work: disabled','localOnly':True})
            if u.path=='/api/export':
                run=platform.team(team)['repo'].get_run(q['run'])
                if not run:raise ValueError('Unknown run')
                return self.send(200,platform.config.scrub(run),headers={'Content-Disposition':f'attachment; filename="{run["id"]}.json"'})
            if u.path=='/api/bundle':return self.send(200,platform.bundle(team,q['run']),'application/zip',{'Content-Disposition':'attachment; filename="crew-products.zip"'})
            if u.path=='/api/artifact':
                a=platform.artifact(team,q['run'],q['id']);return self.send(200,a['content'],'text/plain; charset=utf-8',{'Content-Disposition':"attachment; filename*=UTF-8''"+quote(a['name'])})
            if u.path=='/api/detail':
                run=platform.team(team)['repo'].get_run(q['run'])
                if not run:raise ValueError('Unknown run')
                call=next((c for c in run['calls'] if c['id']==q['id']),None)
                if not call:raise ValueError('Unknown receipt')
                return self.send(200,platform.config.scrub(call))
            if u.path=='/api/benchmarks':
                rows=[]
                for p in sorted((ROOT/'runs').glob('*/measured-results*.json')):
                    try:
                        d=json.loads(p.read_text());rows.append({'name':p.parent.name,'file':p.name,'provider':d.get('provider'),'seconds':d.get('wallSeconds'),'firstPassSeconds':d.get('firstPassWallSeconds'),'cases':[{k:c.get(k) for k in ('caseId','seconds','executionStatus','passedBrowserTests')} for c in d.get('cases',[])],'note':d.get('followupNote') or d.get('limitations')})
                    except (ValueError,OSError):continue
                return self.send(200,rows)
            files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            if u.path not in files:return self.send(404,{'error':'Not found'})
            p=ROOT/'web'/files[u.path];return self.send(200,p.read_bytes(),mimetypes.guess_type(p.name)[0]+'; charset=utf-8')
        except PermissionError as exc:self.send(403,{'error':str(exc)})
        except (ValueError,KeyError,FileNotFoundError) as exc:self.send(400,{'error':platform.config.scrub(str(exc))})
        except Exception as exc:self.send(500,{'error':platform.config.scrub(str(exc))})
    def do_POST(self):
        try:
            self.guard(True)
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('JSON required')
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=131072:raise ValueError('Request size is invalid')
            b=json.loads(self.rfile.read(length));team=b.get('team','software');channel=b.get('channel','group');path=urlsplit(self.path).path
            if path=='/api/start':result=platform.start(team,channel,b['prompt'],b.get('participants'),b.get('mode','task'),b.get('memory',True),b.get('seconds',900),auto_repair=b.get('autoRepair',True));result={'runId':result['id']}
            elif path=='/api/answer':platform.answer(team,b['run'],b['question'],b['answer']);result={'ok':True}
            elif path=='/api/control':platform.control(team,b['run'],b['action'],b.get('reason',''));result={'ok':True}
            elif path=='/api/message':platform.supplement(team,channel,b['text']);result={'ok':True}
            elif path=='/api/reset':platform.reset(team,channel);result={'ok':True}
            elif path=='/api/teams/delete':
                if suite.state and suite.state['status']=='running' and team in suite.state['teams'].values():
                    raise ActionError('This team belongs to a running evaluation.',409)
                result=platform.delete_team(b['team'])
            elif path=='/api/teams':result=platform.create_team(b['name'],b.get('template','default'))
            elif path=='/api/role-prompts':result=platform.set_role_prompts(team,b.get('rolePrompts'));result={'ok':True,'snapshot':result}
            elif path=='/api/agent':platform.update_agent(team,b['agent'],b['values']);result={'ok':True}
            elif path=='/api/settings':platform.settings(team,b['values']);result={'ok':True}
            elif path=='/api/memory':result=platform.confirm_memory(team,b['agent'],b['id'],b['confirmed'])
            elif path=='/api/suite/start':result=suite.start(b.get('seconds',900),b.get('memory',True),b.get('autoHuman',False))
            elif path=='/api/suite/cancel':suite.cancel();result={'ok':True}
            else:return self.send(404,{'error':'Not found'})
            self.send(200,platform.config.scrub(result))
        except PermissionError as exc:self.send(403,{'error':str(exc)})
        except ActionError as exc:self.send(exc.status,{'error':platform.config.scrub(str(exc))})
        except (ValueError,KeyError,TypeError) as exc:self.send(400,{'error':platform.config.scrub(str(exc))})
        except Exception as exc:self.send(500,{'error':platform.config.scrub(str(exc))})
 return Handler

def main():
 p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8100);p.add_argument('--data-dir',type=Path,default=ROOT/'.platform');p.add_argument('--config-root',type=Path,default=ROOT/'config-deepseek');a=p.parse_args()
 platform=Platform(a.data_dir,a.config_root);suite=SuiteManager(platform,ROOT/'fixtures/mid_test')
 server=ThreadingHTTPServer(('127.0.0.1',a.port),handler(platform,suite));print(f'EvoAgent: http://127.0.0.1:{server.server_port}',flush=True)
 try:server.serve_forever()
 except KeyboardInterrupt:pass
 finally:server.server_close()
if __name__=='__main__':main()
