"""Translate a complete DSML tool proposal into CrewAI's ReAct tool syntax."""
import json
import re

TOKEN=r'[｜|]+DSML[｜|]+'
def normalize_dsml(content, allow_handoff=False, allow_review=False, gateway_operations=()):
    if not isinstance(content,str) or 'DSML' not in content:
        return content
    invocation=re.search(r'<'+TOKEN+r'\s+invoke\s+name="([^"]+)"\s*>(.*?)</'+TOKEN+r'\s+invoke\s*>',content,re.S)
    if not invocation:
        raise ValueError('Incomplete DSML tool invocation; return one complete compact ReAct action instead')
    name,body=invocation.groups()
    if name not in gateway_operations and name!='gateway' and not (allow_handoff and name=='handoff') and not (allow_review and name=='verdict'):
        raise ValueError(f'Unavailable DSML tool {name!r}. Use Action: gateway with Action Input containing operation and arguments for an available operation.')
    params={}
    for key,string,value in re.findall(r'<'+TOKEN+r'\s+parameter\s+name="([^"]+)"\s+string="(true|false)"\s*>(.*?)</'+TOKEN+r'\s+parameter\s*>',body,re.S):
        if key in params:raise ValueError('Duplicate DSML tool parameter')
        params[key]=value if string=='true' else json.loads(value,strict=False)
    if name in gateway_operations:
        params={'operation':name,'arguments':params}
        name='gateway'
    if name=='gateway':
        if set(params)!={'operation','arguments'} or not isinstance(params['operation'],str) or not isinstance(params['arguments'],dict):raise ValueError('DSML gateway proposal must contain operation and arguments')
    elif name=='handoff':
        if set(params)-{'paths','summary'} or not isinstance(params.get('paths'),list):raise ValueError('DSML handoff must contain a paths array and optional summary')
    elif set(params)-{'verdict','summary'} or params.get('verdict') not in ('pass','fail'):raise ValueError('DSML verdict must contain pass/fail and summary')
    return 'Thought: Execute one real tool action and wait for its actual receipt.\nAction: '+name+'\nAction Input: '+json.dumps(params,ensure_ascii=False)
