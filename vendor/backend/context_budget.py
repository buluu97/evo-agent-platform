"""Bounded context adaptation from this request's actual visible bodies only.

No ledger/workspace reads, retrieval, new permissions or extra model calls.
Projection happens before replacement, so an expansion cannot claim to recover
bytes already discarded by an earlier request.
"""
import copy
import json

from .runtime import ProtocolError, _working_tool_results, compress_messages
from .web_context import collect_web_records


def visible_bodies(messages):
    records,files=_working_tool_results(messages)
    rows=[]
    for key,record in files.items():
        if not (record.get('writtenBody') or record.get('fullRead')):continue
        value=record['value'];mode='full' if record.get('writtenBody') else value.get('mode','full')
        rows.append({'kind':'file','path':key[0],'sha256':key[1],
                     'window':'full' if mode=='full' else key[2:],
                     'sourceAction':'file.write' if record.get('writtenBody') else 'file.read'})
    for record in collect_web_records(records):
        identity=record['webIdentity']
        if identity['bodyPresent']:
            rows.append({'kind':'web','url':identity['url'],'sha256':identity['contentSha256']})
    return rows


def missing_bodies(before,after):
    missing=[]
    for source in before:
        if source['kind']=='web':
            covered=any(row['kind']=='web' and row['url']==source['url'] and row['sha256']==source['sha256'] for row in after)
        else:
            covered=any(row['kind']=='file' and row['path']==source['path'] and row['sha256']==source['sha256']
                        and (row['window']=='full' or row['window']==source['window']) for row in after)
        if not covered:missing.append(source)
    return missing


def _initial_status(messages,status):
    """Only the server-shaped initial context; never rewrite arbitrary user text."""
    if len(messages)<2:return messages
    try:context=json.loads(messages[1].get('content',''))
    except (ValueError,TypeError):return messages
    if not (isinstance(context,dict) and isinstance(context.get('task'),dict)
            and isinstance(context['task'].get('id'),str)):return messages
    context['contextCapacity']=status
    return [messages[0],{**messages[1],'content':json.dumps(context,ensure_ascii=False,separators=(',',':'))},*messages[2:]]


def _fit_projection(candidate,limit,protect_projection,trusted_feedback=None):
    """Reserve the measured cost of server-owned post-compaction protection.

    Each attempt starts from the same original candidate: a retry must not lose
    bodies merely because an earlier trial projected them out. Protection may
    restore a real question/reply pair or reserialize the protected initial
    context. Its bytes count against the same ceiling, never an extra allowance.
    """
    compression_limit=limit
    for attempt in range(8):
        trial=compress_messages(candidate,compression_limit,trusted_feedback=trusted_feedback)
        compressed_chars=sum(len(item.get('content','')) for item in trial)
        if protect_projection is not None:trial=protect_projection(trial)
        final_chars=sum(len(item.get('content','')) for item in trial)
        if final_chars<=limit:
            return trial,{'compressionChars':compression_limit,'projectionPasses':attempt+1,
                          'projectionReserveChars':limit-compression_limit}
        # Account for the actual added bytes, including a tail restored only
        # after compaction. Strictly decrease the compression allowance, even
        # when the selected whole-message set changes at a boundary.
        overhead=max(0,final_chars-compressed_chars)
        compression_limit=min(compression_limit-(final_chars-limit),limit-overhead)
        if compression_limit<=0:break
    raise ProtocolError('上下文容量不足以完整保留受保护的真实问答及任务索引；未发送超限或缺失回答的请求。')


def adapt_context(messages,current_limit,ceiling=80000,initial=40000,protect_projection=None,trusted_feedback=None):
    """Return a copied projection and bounded audit state, without mutating input."""
    ceiling=int(ceiling);initial=min(int(initial),ceiling)
    active=min(ceiling,max(initial,int(current_limit or initial)))
    limits=sorted({active,*[limit for limit in (40000,60000,80000) if active<limit<=ceiling],ceiling})
    before=visible_bodies(messages);attempts=[];projected=None;missing=[]
    for limit in limits:
        # Include the exact short notice in the trial, rather than reserve an
        # arbitrary margin which could itself evict a source and cause growth.
        notice={'activeChars':limit,'ceilingChars':ceiling,'scope':'context_only',
                'nextStep':'If a receipt marks body omitted, read only missing authorized windows or split work; omitted text is not evidence. Time and permissions are unchanged.'}
        candidate=_initial_status(messages,notice)
        try:
            trial,projection=_fit_projection(candidate,limit,protect_projection,trusted_feedback)
        except ProtocolError as error:
            if not str(error).startswith('上下文容量不足'):raise
            attempts.append({'limit':limit,'indexCapacityError':True})
            if limit==ceiling:raise
            continue
        missing=missing_bodies(before,visible_bodies(trial))
        attempts.append({'limit':limit,'missingBodies':len(missing),
                         **(projection if projection['projectionReserveChars'] else {})})
        projected=trial
        if not missing:break
    state={'initialChars':initial,'activeChars':limit,'ceilingChars':ceiling,
           'visibleBodiesBefore':len(before),'missingBodiesAfter':len(missing),
           'missingBodyReferences':copy.deepcopy(missing[:6]),'missingReferencesTruncated':len(missing)>6,
           'attempts':attempts,'scope':'context_only',
           'note':'Actual working-message bodies only; time, output and permissions unchanged.'}
    if sum(len(item.get('content','')) for item in projected)>limit:
        raise ProtocolError('上下文容量不足以保留真实容量状态；未发送超限上下文。')
    state['characters']=sum(len(item.get('content','')) for item in projected)
    return projected,state
