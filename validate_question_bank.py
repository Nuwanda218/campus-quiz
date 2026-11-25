#!/usr/bin/env python3
from __future__ import annotations
import json,re,sys,unicodedata
from collections import Counter,defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parent
PATH=ROOT/'data'/'questions.json'

def norm(text:str)->str:
    text=unicodedata.normalize('NFKC',text)
    return re.sub(r'[\W_]+','',text,flags=re.UNICODE).lower()

def main()->int:
    qs=json.loads(PATH.read_text(encoding='utf-8'))
    errors=[]; seen={}; normalized=defaultdict(list)
    for i,q in enumerate(qs,1):
        qid=str(q.get('id',''))
        if not qid: errors.append(f'第{i}题缺少ID')
        if qid in seen: errors.append(f'重复ID：{qid}')
        seen[qid]=i
        question=str(q.get('question',''))
        if not question.strip(): errors.append(f'{qid}题干为空')
        labels=[str(o.get('label','')).upper() for o in q.get('options',[])]
        if len(labels)<2 or len(labels)!=len(set(labels)): errors.append(f'{qid}选项标签异常')
        answer=''.join(sorted(set(str(q.get('answer','')).upper())))
        if not answer or not set(answer)<=set(labels): errors.append(f'{qid}答案不在选项中：{answer}')
        if q.get('type')=='single' and len(answer)!=1: errors.append(f'{qid}单选答案数量异常：{answer}')
        if q.get('type')=='multi' and len(answer)<2: errors.append(f'{qid}多选答案数量异常：{answer}')
        if q.get('type') not in {'single','multi'}: errors.append(f'{qid}题型异常')
        texts=[question]+[str(o.get('text','')) for o in q.get('options',[])]+[str(q.get('explanation',''))]
        for text in texts:
            if '\ufffd' in text: errors.append(f'{qid}含Unicode替换字符')
            if any(ord(ch)<32 and ch not in '\n\t\r' for ch in text): errors.append(f'{qid}含控制字符')
        normalized[norm(question)].append(qid)
        if qid.startswith('nq'):
            if q.get('audit_rounds')!=2: errors.append(f'{qid}未标记两轮审核')
            if not q.get('textbook_evidence'): errors.append(f'{qid}缺少教材证据')
            if not q.get('authoritative_sources'): errors.append(f'{qid}缺少权威来源')
            if not q.get('explanation'): errors.append(f'{qid}缺少解析')
    exact={k:v for k,v in normalized.items() if k and len(v)>1}
    if exact:
        for ids in exact.values(): errors.append('题干完全重复：'+','.join(ids))
    print(f'题目总数：{len(qs)}')
    print('题型：'+str(dict(Counter(q.get('type') for q in qs))))
    print(f'新增题：{sum(str(q.get("id","")).startswith("nq") for q in qs)}')
    if errors:
        print('\n'.join(errors),file=sys.stderr); return 1
    print('校验通过：ID、题型、答案映射、重复题、乱码、控制字符及新增题审核字段均正常。')
    return 0
if __name__=='__main__': raise SystemExit(main())
