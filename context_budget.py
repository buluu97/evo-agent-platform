"""Bound old chat history while retaining current file observations in full."""
def bound_messages(messages, limit=120000):
    if sum(len(str(m.get('content',''))) for m in messages)<=limit:
        return messages
    first=[dict(m) for m in messages[:2]]
    for m in first:
        text=str(m.get('content',''))
        if len(text)>20000:
            m['content']=text[:12000]+'\n[Older task context abbreviated; use actual files.]\n'+text[-7500:]
    note={'role':'user','content':'Older intermediate turns were omitted to bound context. Recent actual file/tool observations below remain authoritative. Do not repeatedly reread unchanged files; use the information already returned to complete the current task.'}
    budget=limit-sum(len(str(m.get('content',''))) for m in first)-len(note['content'])
    tail=[]
    for original in reversed(messages[2:]):
        m=dict(original);text=str(m.get('content',''))
        if len(text)>40000:
            text=text[:12000]+'\n[Oversized historical observation abbreviated; inspect the source file if needed.]\n'+text[-27000:]
        if len(text)>budget:
            if not tail:
                m['content']=text[-max(0,budget-100):]
                tail.insert(0,m)
            break
        m['content']=text;tail.insert(0,m);budget-=len(text)
    return first+[note]+tail
