"""Evidence-bound read-only phase admission; never a source or permission grant."""
import hashlib
import re
from urllib.parse import urlsplit

from .context_budget import visible_bodies
from .stage_control import _unresolved_owner_effects
from .web_context import normalize_result_url

_PHASES={'synthesis','handoff_final','local_synthesis_recovery'}
_URL=re.compile(r'''https?://[^\s<>"'`]+''')
_MARKDOWN_URL=re.compile(r'''\[[^\]\r\n]{0,500}\]\((https?://[^()\s<>"']+)\)''')

def _observed_urls(text):
    # Exact plain/angle-delimited tokens, plus explicit Markdown link targets.
    # Never strip legal URL punctuation or admit a path/query prefix.
    return {m.group(0) for m in _URL.finditer(text)}|{m.group(1) for m in _MARKDOWN_URL.finditer(text)}


def _url(value):
    if not isinstance(value,str) or not 1<=len(value)<=2048:return None
    try:
        value.encode('utf-8');p=urlsplit(value)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:return None
        if any(ord(c)<33 for c in value) or '\\' in value:return None
        p.port
    except (ValueError,UnicodeError):return None
    return value


def necessary_source_admission(run,task,action,agents,control,now_ms,*,finalization_window_ms=120000):
    """Return a narrow admission receipt only for a real known URL and stated gap.

    The caller still runs stage-independent ACL, URL/SSRF, approval, retry and
    deadline checks. A receipt does not execute a call, supply a body, assert
    freshness/quality or prove that a previously fetched body is still visible.
    """
    if (not isinstance(control,dict) or control.get('phase') not in _PHASES
            or run.get('status')!='running' or run.get('mode')!='task'
            or (run.get('budget') or {}).get('profile')!='time_only'
            or type(now_ms) is not int or type(finalization_window_ms) is not int
            or not 30000<=finalization_window_ms<=120000):return None
    deadline=(run.get('budget') or {}).get('deadlineAt');revision=task.get('revision',0)
    if (type(deadline) is not int or now_ms>=deadline-finalization_window_ms
            or type(revision) is not int or revision<0
            or task.get('status')!='running' or not any(t is task for t in run.get('steps',[]))
            or any(control.get(k)!=v for k,v in {'runId':run.get('id'),'taskId':task.get('id'),
                'agentId':task.get('agentId'),'revision':revision,'runDeadlineAt':deadline}.items())
            or type(control.get('revision')) is not int
            or any(type(control.get(k)) is not int for k in ('allocatedAt','synthesisAt','handoffAt','expectedHandoffBy'))
            or not control['allocatedAt']<=control['synthesisAt']<=control['handoffAt']<=control['expectedHandoffBy']<=deadline
            or now_ms<control['synthesisAt']):return None
    actor=task.get('agentId');agent=agents.get(actor,{})
    if 'web.fetch' not in agent.get('tools',[]) or action.get('type')!='tool' or action.get('name')!='web.fetch':return None
    args=action.get('args');gap=action.get('evidenceGap')
    if not isinstance(args,dict) or set(args)!={'url'} or not _url(args.get('url')):return None
    if not isinstance(gap,str) or not 8<=len(gap.strip())<=600 or '\0' in gap:return None
    try:gap.encode('utf-8')
    except UnicodeError:return None
    # Unknown side effects still require their existing reconciliation path.
    if _unresolved_owner_effects(run,task):return None
    target=args['url'];proof=None
    # Only host-recorded original human messages, never agent titles or prose.
    for row in run.get('transcript',[]):
        if (row.get('runId')==run.get('id') and row.get('role')=='human' and not row.get('agentId')
                and isinstance(row.get('id'),str) and isinstance(row.get('text'),str)):
            urls=_observed_urls(row['text'])
            if target in urls:
                try:digest=hashlib.sha256(row['text'].encode('utf-8')).hexdigest()
                except UnicodeError:continue
                proof={'kind':'human_url','messageId':row['id'],'sourceTextSha256':digest};break
    # Tool links come only from this task/owner/revision's actual successful call.
    if proof is None:
        for call in reversed(run.get('calls',[])):
            result=call.get('result')
            if (call.get('taskId')!=task.get('id') or call.get('agentId')!=actor
                    or type(call.get('revision')) is not int or call['revision']!=revision
                    or call.get('runId',run.get('id'))!=run.get('id')
                    or call.get('tool') not in ('web.fetch','web.search','file.read') or call.get('status')!='success'
                    or not isinstance(call.get('id'),str) or not isinstance(result,dict) or result.get('ok') is not True
                    or result.get('outcomeUnknown') or result.get('deadlineExceeded') or result.get('modelViewOnly')
                    or result.get('notExecuted') or result.get('actionExecuted') is False):continue
            if call['tool']=='file.read':
                read_args=call.get('args');mode=result.get('mode')
                if (not isinstance(read_args,dict) or not isinstance(read_args.get('path'),str)
                        or result.get('path')!=read_args['path'] or result.get('contentOmitted')
                        or not isinstance(result.get('sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',result['sha256'])):
                    continue
                parts=([result['content']] if mode in ('full','lines') and isinstance(result.get('content'),str) else
                       [match['content'] for match in result.get('matches',[]) if isinstance(match,dict) and isinstance(match.get('content'),str)]
                       if mode=='find' and isinstance(result.get('matches'),list) else [])
                if any(target in _observed_urls(part) for part in parts):
                    proof={'kind':'successful_file_read_url','callId':call['id'],'tool':'file.read',
                           'path':read_args['path'],'sha256':result['sha256'],'mode':mode}
                    if mode=='lines':
                        proof.update({key:result[key] for key in ('startLine','endLine','startColumn','endColumn') if key in result})
                    break
                continue
            field='results' if call['tool']=='web.search' else 'links'
            links=result.get(field,[])
            found=isinstance(links,list) and any(isinstance(x,dict) and x.get('url')==target for x in links)
            # A lost body may be re-read through an available private snapshot;
            # past successful fetch alone never proves current body visibility.
            previous_body=call['tool']=='web.fetch' and result.get('url')==target and isinstance(result.get('content'),str)
            if found or previous_body:
                proof={'kind':'successful_tool_link' if found else 'prior_successful_fetch_url','callId':call['id'],
                       'tool':call['tool'],'field':field if found else 'url'};break
    if proof is None:return None
    # The current working view, not history/counters, decides whether rereading
    # the exact known latest body would merely acquire bytes already visible.
    latest=next((c for c in reversed(run.get('calls',[])) if c.get('taskId')==task['id']
        and c.get('agentId')==actor and type(c.get('revision')) is int and c['revision']==revision
        and c.get('tool')=='web.fetch' and c.get('status')=='success'
        and isinstance(c.get('result'),dict) and c['result'].get('ok') is True
        and (normalize_result_url(c['result'].get('url'))==normalize_result_url(target)
             or isinstance(c.get('args'),dict) and normalize_result_url(c['args'].get('url'))==normalize_result_url(target))
        and isinstance(c['result'].get('content'),str)),None)
    if latest is not None:
        final_identity=normalize_result_url(latest['result'].get('url'))
        # An alias may have been read before a newer direct fetch of its final
        # URL. Only the latest real version at that identity can suppress I/O.
        latest=next((c for c in reversed(run.get('calls',[])) if c.get('taskId')==task['id']
            and c.get('agentId')==actor and type(c.get('revision')) is int and c['revision']==revision
            and c.get('tool')=='web.fetch' and c.get('status')=='success'
            and isinstance(c.get('result'),dict) and c['result'].get('ok') is True
            and normalize_result_url(c['result'].get('url'))==final_identity),latest)
        if not isinstance(latest['result'].get('content'),str):latest=None
    if latest is not None:
        try:digest=hashlib.sha256(latest['result']['content'].encode('utf-8')).hexdigest()
        except UnicodeError:return None
        messages=task.get('_messages',[])
        bodies=visible_bodies(messages) if isinstance(messages,list) else []
        final_url=normalize_result_url(latest['result'].get('url'))
        if final_url and any(row.get('kind')=='web' and row.get('url')==final_url
                and row.get('sha256')==digest for row in bodies):
            return None
        snapshot=latest['result'].get('sourceSnapshot')
        if (isinstance(snapshot,dict) and snapshot.get('sha256')==digest
                and isinstance(snapshot.get('path'),str)
                and any(row.get('kind')=='file' and row.get('window')=='full'
                        and row.get('path')==snapshot['path'] and row.get('sha256')==digest for row in bodies)):
            return None
    return {'admitted':True,'actionExecuted':False,'admissionOnly':True,'runId':run['id'],'taskId':task['id'],
            'agentId':actor,'revision':revision,'phase':control['phase'],'url':target,'evidenceGap':gap.strip(),
            'sourceProof':proof,'admittedAt':now_ms,'remainingMs':deadline-now_ms,'deadlineAt':deadline,
            'instruction':'Only this requested known-URL fetch passes the phase gate. Original ACL/SSRF/deadline and '
                'permanent-failure guards still apply; this receipt is neither fetched content nor verified evidence. '
                'Prefer an available authorized source snapshot when its body is omitted. No search, permission, time or acceptance is added.'}
