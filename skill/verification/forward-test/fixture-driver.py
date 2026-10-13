# coding: utf-8
from pathlib import Path
import tempfile, os, json, hashlib, subprocess, datetime, atexit, argparse, shutil

parser=argparse.ArgumentParser(description='仅合成阶段/调用边界验证；不执行题目代码，退出自动清理临时树。')
parser.add_argument('--archive-out',type=Path,help='可选：将纯合成回执归档到此目录，然后清理临时树。')
args=parser.parse_args()

suite=Path(__file__).resolve().parents[2]
scratch=Path(os.environ.get('TMPDIR','/tmp'))/'zcode-scratch'/'autoresearch-package-skills'
scratch.mkdir(parents=True,exist_ok=True)
root=Path(tempfile.mkdtemp(prefix='forward-',dir=str(scratch)))
atexit.register(shutil.rmtree,root,ignore_errors=True)
package=root/'package'; review=root/'review'; review.mkdir()

def put(rel,data):
    p=package/rel; p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(data,ensure_ascii=False,indent=2) if isinstance(data,dict) else data,encoding='utf-8')
    return p

for rel in ['workspace/harbor_task/instruction.md','workspace/harbor_task/task.toml','workspace/harbor_task/environment/Dockerfile','workspace/harbor_task/tests/Dockerfile','workspace/harbor_task/tests/test.sh','expert_evidence/专家作业说明文档.md','optimization_evidence/训练证据说明.md']:
    put(rel,'SYNTHETIC FIXTURE ONLY; deliberately not executable or valid scientific evidence.\n')
(package/'workspace/reference').mkdir(parents=True)
put('expert_evidence/expert_annotation.json',{'fixture':True})
put('expert_evidence/run_summary.json',{'status':'NOT_RUN','agent_execution':[{'runtime':'Codex','model':m,'duration_hours':None,'effective_rounds':None,'typical_round_minutes':None} for m in ['GPT-5.6 Sol','Seed 2.1 Turbo']],'validation':{'best_method_rerun_passed':None}})
for n in ['trajectory_codex','trajectory_seed']:
    put('expert_evidence/'+n+'.json',{'rounds':[]})
for role in ['baseline','reference']:
    for seed in [1,2,3]:
        stem=f'optimization_evidence/{role}_runs/seed_{seed}'
        put(stem+'/result.json',{'schema_version':'synthetic.v1','status':'COMPLETE','role':role,'seed':seed,'task_type':'model_training','method':{'fixture':True},'protocol':{'fixture':True},'training':{'fixture':True},'execution':{'fixture':True},'metrics':{'value':0.5},'quality_gate':{'fixture':True},'artifacts':{'fixture':True}})
        put(stem+'/run.log','SYNTHETIC FIXTURE: no training was executed.\n')
        put(stem+'/model/model.bin','SYNTHETIC NON-MODEL BYTES; must never load.\n')
        put(stem+'/model/artifact.json',{'fixture':True})
put('optimization_evidence/comparison_summary.json',{'schema_version':'synthetic.v1','seeds':[1,2,3],'status':'COMPLETE','fixture':True})

def run(script,args,name):
    cmd=['python3','-B',str(suite/'scripts'/script),*args]
    p=subprocess.run(cmd,capture_output=True,text=True)
    out=review/name;out.write_text(p.stdout,encoding='utf-8')
    receipt={'argv':cmd,'exit_code':p.returncode,'output_path':str(out),'output_sha256':hashlib.sha256(p.stdout.encode()).hexdigest()}
    if not p.stdout.strip():
        raise RuntimeError({'receipt':receipt,'stderr':p.stderr})
    return receipt,json.loads(p.stdout)

receipt,obs=run('check_package.py',['--root',str(package),'--mode','pre_run'],'initial.json')
digest=obs['facts']['snapshot']['sha256']
ids=['trajectory_codex','trajectory_seed','best_method','agent_effective_duration','agent_round_count','best_method_clean_revalidation']
items=[]
for item_id in ids:
    target='expert_evidence/'+item_id+'.json' if item_id.startswith('trajectory_') else 'expert_evidence/best_method' if item_id=='best_method' else 'expert_evidence/run_summary.json'
    items.append({'id':item_id,'layer':'F','target':target,'required_stage':'post_run','status':'合理待生成','value':None,'reason':'合成请求处于G03后长程前；未运行研究Agent','sources':[],'action':'另行授权后由真实长程产生','authorization_required':True})
ledger={'schema_version':'package-ledger.v1','mode':'pre_run','package_digest':digest,'items':items,'layers':{},'pending_long_run':ids}
ledger_path=review/'qa-ledger.json';ledger_path.write_text(json.dumps(ledger,ensure_ascii=False,indent=2),encoding='utf-8')
pending_receipt,pending=run('check_package.py',['--root',str(package),'--mode','pre_run','--ledger',str(ledger_path)],'pending.json')
assert not [r for r in pending['findings'] if r['check'].startswith('F') and r['status']=='不通过']
assert len([r for r in pending['findings'] if r['status']=='合理待生成'])==6
assert len([r for r in pending['findings'] if r['check']=='E05' and r['status']=='证据不足'])==6

invalid=json.loads(json.dumps(ledger))
invalid['items'].append({'id':'E05.reload','layer':'E','target':'optimization_evidence/baseline_runs/seed_1/model/reload.log','required_stage':'post_run','status':'合理待生成','value':None,'reason':'试图错误延期正式重载','sources':[],'action':'待生成','authorization_required':False})
invalid['pending_long_run'].append('E05.reload')
bad_ledger=review/'illegal-ledger.json';bad_ledger.write_text(json.dumps(invalid,ensure_ascii=False,indent=2),encoding='utf-8')
illegal_receipt,illegal=run('check_package.py',['--root',str(package),'--ledger',str(bad_ledger)],'illegal.json')
assert illegal_receipt['exit_code']==1 and any(r['check']=='A06' and r['status']=='不通过' for r in illegal['findings'])

route_receipt,route=run('review_contract.py',['routes'],'routes.json')
routes={r['layer']:r for r in route['routes']}
def hashfile(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def layer_doc(letter,statuses,conclusion):
    row=routes[letter];skill=suite/row['path'];source=suite/'references/requirements-map.md'
    evidence_path='expert_evidence/run_summary.json' if letter=='F' else 'optimization_evidence/comparison_summary.json'
    checks=[]
    for item_id in row['checks']:
        status=statuses.get(item_id,'证据不足')
        checks.append({'id':item_id,'status':status,'clause':'requirements-map.md '+item_id,'evidence':[{'path':evidence_path,'sha256':hashfile(package/evidence_path),'locator':'#fixture' if letter=='E' else '#status'}],
                       'fact':'合成夹具无正式独立重载或有效科学材料' if letter=='E' else '两轨rounds=[]且run_summary实际统计为null；原因在包外台账',
                       'reason':'真实长程未执行，六类post_run产物待生成' if status=='合理待生成' else '不是真实科学/平台证据，不能放行',
                       'action':'补挂已有真实证据；本轮不执行实验' if letter=='E' else '保留post_run原因与目标Schema空值；不造长程成果',
                       'recheck':'真实来源/版本/调用/指标记录' if letter=='E' else '目标Schema及后续真实长程记录'})
    return {'schema_version':'package-layer-review.v1','mode':'pre_run','layer':letter,'package_digest':digest,'skill':{'path':row['path'],'sha256':hashfile(skill)},
            'source_basis':[{'path':'references/requirements-map.md','sha256':hashfile(source),'locator':letter+' checks'}],
            'commands':[' '.join(pending_receipt['argv'])+'; exit='+str(pending_receipt['exit_code'])+'; output='+pending_receipt['output_path']+'; sha256='+pending_receipt['output_sha256']],
            'command_receipts':[pending_receipt],
            'definition':{'deliverables':['合成边界夹具；不得充作正式科研证据'],'acceptance':['核待生成边界，严格保留真实缺口'],'risks':['夹具不包含可加载真模型']},
            'checks':checks,'conclusion':conclusion,'risks':['上游报告尚未提供，不能越层放行'],'upstream':[]}

e=layer_doc('E',{'E05':'不通过','E07':'不通过'},'不通过')
f=layer_doc('F',{'F03':'合理待生成','F04':'合理待生成','F05':'合理待生成'},'证据不足')
valid_receipts=[]
for letter,doc in [('E',e),('F',f)]:
    p=review/f'layer-{letter}.json';p.write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding='utf-8')
    r,c=run('review_contract.py',['layer','--root',str(package),'--report',str(p)],f'contract-{letter}.json')
    assert r['exit_code']==0 and c['contract_valid'] and not c['layers_allow_admission']
    valid_receipts.append(r)
bad_e=json.loads(json.dumps(e));next(r for r in bad_e['checks'] if r['id']=='E05')['status']='合理待生成'
p=review/'layer-E-illegal.json';p.write_text(json.dumps(bad_e,ensure_ascii=False,indent=2),encoding='utf-8')
bad_e_receipt,bad_e_result=run('review_contract.py',['layer','--root',str(package),'--report',str(p)],'contract-illegal-E.json')
assert bad_e_receipt['exit_code']==1 and any('不能延期' in x for r in bad_e_result['reports'] for x in r['issues'])
bad_f=json.loads(json.dumps(f));bad_f['conclusion']='通过'
p=review/'layer-F-bypass.json';p.write_text(json.dumps(bad_f,ensure_ascii=False,indent=2),encoding='utf-8')
bad_f_receipt,bad_f_result=run('review_contract.py',['layer','--root',str(package),'--report',str(p)],'contract-bypass-F.json')
assert bad_f_receipt['exit_code']==1 and any('越层通过' in x for r in bad_f_result['reports'] for x in r['issues'])
# 完整加载的授权核验约定：没有目标版本/CLI/provider材料，不猜NOP命令。
plan={'clause':'authorized-verification.md 命令如何成为准确、可审核的命令§2',
      'status':'COMMAND_UNRESOLVED','purpose':'当前版本NOP准确命令',
      'argv':None,'reason':'合成输入没有目标Harbor版本、该版本CLI help/源码与provider映射',
      'executed':False,'admission_effect':'证据不足，保持阻塞'}
plan_path=review/'verification-plan.json';plan_path.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
h=layer_doc('H',{},'证据不足')
for row in h['checks']:
    row['fact']='最小合成输入不含当前版本NOP、完整评分与目标工具接入回执'
    row['reason']='COMMAND_UNRESOLVED：无法由实际目标版本与CLI/provider证据得到准确核验命令'
    row['action']='补挂版本/CLI help/源码/现有配置；仅给准确核验单，本轮不执行'
    row['recheck']='真实目标版本与配置/准确argv/同Trial证据'
    row['evidence']=[{'path':'@review/verification-plan.json','sha256':hashfile(plan_path),'locator':'#status'}]
h_path=review/'layer-H.json';h_path.write_text(json.dumps(h,ensure_ascii=False,indent=2),encoding='utf-8')
h_receipt,h_result=run('review_contract.py',['layer','--root',str(package),'--report',str(h_path)],'contract-H-unresolved.json')
assert h_receipt['exit_code']==0 and h_result['contract_valid'] and not h_result['layers_allow_admission']
final_receipt,final_result=run('review_contract.py',['final','--root',str(package),'--reports',str(review),'--mode','pre_run'],'final-contract.json')
assert final_receipt['exit_code']==1 and not final_result['contract_valid'] and not final_result['layers_allow_admission']
summary={'completed_at':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),'fixture_root':str(root),'package_digest':digest,
         'structure_digest':pending['facts']['structure_manifest']['sha256'],'receipts':[receipt,pending_receipt,illegal_receipt,route_receipt,*valid_receipts,bad_e_receipt,bad_f_receipt,h_receipt,final_receipt],
         'counts':{'pending':6,'missing_reload':6,'F_failure':0},
         'skills':[{'path':str(suite/p),'sha256':hashfile(suite/p)} for p in ['autoresearch-package-finalize/SKILL.md','layers/autoresearch-package-layer-e-evidence/SKILL.md','layers/autoresearch-package-layer-f-expert/SKILL.md','autoresearch-package-final-audit/SKILL.md']]}
summary['cleanup']={'method':'atexit:定向删除本次mkdtemp树，包括异常退出路径','scheduled':True,'retained_package':False}
(review/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
if args.archive_out:
    out=args.archive_out.resolve()
    if out.is_relative_to(root):raise ValueError('归档不能位于待清理树内')
    out.mkdir(parents=True,exist_ok=True)
    for p in review.iterdir():
        if p.is_file():shutil.copy2(p,out/p.name)
print(json.dumps(summary,ensure_ascii=False,indent=2))
