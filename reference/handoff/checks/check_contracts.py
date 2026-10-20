"""Validate specification fixtures, not the running application or model accuracy."""
from pathlib import Path
from copy import deepcopy
import json, sqlite3, re, unicodedata
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT=Path(__file__).resolve().parents[1]
assert FormatChecker().conforms('2027-02-30', 'date') is False
assert FormatChecker().conforms('not-a-timestamp', 'date-time') is False
def read(p):return json.loads((ROOT/p).read_text(encoding='utf-8'))
domain=read('contracts/domain.schema.json');Draft202012Validator.check_schema(domain)
registry=Registry().with_resource(domain['$id'],Resource.from_contents(domain))
negative_cases=0
def valid(name,data):
 v=Draft202012Validator({'$ref':domain['$id']+'#/$defs/'+name},registry=registry,format_checker=FormatChecker())
 errors=list(v.iter_errors(data))
 if errors:raise AssertionError(name+': '+str(errors[0]))
def invalid(name,data):
 global negative_cases
 try:valid(name,data)
 except AssertionError:negative_cases+=1;return
 raise AssertionError('Invalid shape accepted: '+name)
manifest=read('contracts/examples/manifest.json')
data={f:read('contracts/examples/'+f) for f in manifest['examples']}
for f,n in manifest['examples'].items():valid(n,data[f])
probe=read('evidence/probe-findings.json');valid('ReportedProbeSummary',probe)
counts=probe['http_url_counts'];assert counts['attempted']==counts['expected_material']+counts['content_mismatch']+counts['connection_failure']==10
assert counts['pdf_included']==2 and probe['raw_artifacts_inspected_here'] is False
assert probe['models']['initial_text_cases']+probe['models']['initial_image_cases']==8
assert probe['scope']=='individual_operations_manual_probe' and not probe['external_extraction_executed']

c=data['candidate.example.json']
b=deepcopy(c);b['candidates'][0]['date_evidence']=[];invalid('CandidateEnvelope',b)
b=deepcopy(c);b['candidates'][0]['event_date']['date']='2027-02-30';invalid('CandidateEnvelope',b)
b=deepcopy(c);b['candidates'][0]['start_time']='25:00';invalid('CandidateEnvelope',b)
b=deepcopy(c);b['candidates'][0]['time_evidence']=[];invalid('CandidateEnvelope',b)
b=deepcopy(c);b['candidates'][0]['event_date']['precision']='month';invalid('CandidateEnvelope',b)
b=deepcopy(c);b['candidates'][0]['event_date']={'precision':'month','date':None,'month':'2027-13'};invalid('CandidateEnvelope',b)
b=deepcopy(c);b['candidates'][0]['event_date']={'precision':'unknown','date':'2027-01-07','month':None};invalid('CandidateEnvelope',b)
b=deepcopy(c);b['official']=True;invalid('CandidateEnvelope',b)
b=deepcopy(c);b['candidates'][0]['context_evidence'][0]['kind']='image_region';invalid('CandidateEnvelope',b)
p=data['publication-ready.example.json']
b=deepcopy(p);b['fields']['event_date']['automatic']['value']=None;invalid('EventProjection',b)
b=deepcopy(p);b['fields']['event_date']['automatic']['value_check']['status']='unchecked';invalid('EventProjection',b)
b=deepcopy(p);b['fields']['start_time']['automatic']['bound_date']=None;invalid('EventProjection',b)
b=deepcopy(p);b['publication']['state']='hold';invalid('EventProjection',b)
b=deepcopy(data['projection.example.json']);b['publication']['calendar_time']='10:00';invalid('EventProjection',b)
b=deepcopy(data['http200-missing.example.json']);b['status']='ok';invalid('DocumentRead',b)
b=deepcopy(data['calendar-sample.example.json']);b['coverage_complete']=True;invalid('CalendarResult',b)
b=deepcopy(data['manual-patch.example.json']);b['operations'][0]['resolves_fingerprint']='current';b['operations'][0]['confirmed_at']=None;invalid('OverridePatch',b)

def norm(s):return ' '.join(unicodedata.normalize('NFC',s).split())
documents={x['id']:x for f,x in data.items() if manifest['examples'][f]=='DocumentRead'}
quote_count=0
for f,x in data.items():
 if manifest['examples'][f]!='CandidateEnvelope':continue
 doc=documents[x['document_id']];segments={s['id']:s for s in doc['segments']}
 assert doc['institution_id']==x['institution_id']
 for candidate in x['candidates']:
  for k in ['context_evidence','date_evidence','time_evidence','venue_evidence']:
   for e in candidate[k]:
    if e['kind']=='literal':
     assert norm(e['quote']) in norm(segments[e['segment_id']]['text']),(f,k,e)
     quote_count+=1
# Deliberately combined noncontiguous snippets must not be accepted as one literal quote.
md=data['month-document.example.json'];fake_quote='2027년 2월 졸업식'
assert not any(norm(fake_quote) in norm(s['text']) for s in md['segments'])

# Recompute fixtures through the same reference function used by the adversarial suite.
from publication_policy import calculate_publication
observations={x['id']:x for f,x in data.items() if manifest['examples'][f]=='Observation'}
for f,e in data.items():
 if manifest['examples'][f]!='EventProjection':continue
 season={'id':e['season_id'],'academic_year':2026,'search_start':'2026-12-01','search_end':'2027-03-31'}
 context={'documents':documents,'observations':observations,'registered_rules':{'explicit_notice':'1.0.0'},'now':'2026-10-20T07:00:00Z','event_blockers':[],'use_fixture_rules':True}
 if f=='failed-recheck-projection.example.json':context=data['failed-recheck-context.example.json']
 assert e['publication']==calculate_publication(e,season,context), f
assert not data['month-projection.example.json']['publication']['calendar_eligible']
assert data['projection.example.json']['publication']['calendar_date']=='2027-01-07'
assert data['projection.example.json']['publication']['calendar_time'] is None
assert data['publication-ready.example.json']['publication']['sales_ready']
# Preserved evidence files must match their recorded hash, without needing original paths.
import hashlib
for entry in read('evidence/manifest.json')['files']:
 assert hashlib.sha256((ROOT/'evidence'/entry['file']).read_bytes()).hexdigest()==entry['sha256'],entry['file']
defaults=read('config/defaults.json');mapping=read('config/env-map.json');valid('ExecutionLimits',defaults['execution'])
env={}
for line in (ROOT/'.env.example').read_text().splitlines():
 line=line.strip()
 if not line or line.startswith('#'):continue
 k,v=line.split('=',1);assert k not in env;env[k]=v
assert set(env) <= set(mapping)
assert set(env)=={key for key,definition in mapping.items() if definition.get('user_setup')}
assert len(env)==6
assert env['OPENAI_API_KEY']==''
def lookup(p):
 x=defaults
 for k in p.split('.'):
  if k not in x:return ''
  x=x[k]
 return str(x).lower() if isinstance(x,bool) else str(x)
for k,m in mapping.items():
 if not m.get('secret'):
  value=defaults
  for part in m['path'].split('.'):
   assert part in value,(k,'missing default path')
   value=value[part]
 if k in env:
  assert env[k]==lookup(m['path']),(k,'template differs from public defaults')
  if m.get('secret'):assert env[k]==''
assert 'secrets' not in defaults
from configuration_reference import can_resume_configuration, resolve_configuration
resolved=resolve_configuration(defaults,mapping,env)
assert resolved['execution']==defaults['execution'] and resolved['secrets']=={}
assert can_resume_configuration(data['job.example.json']['scope_snapshot'],resolved)
assert defaults['app']['llm_backend']=='ollama'
assert defaults['external_transfer']['document_text_allowed'] is False and defaults['external_transfer']['image_allowed'] is False
assert defaults['models']['openai_fast_model']
assert set(defaults['llm_profiles'])==set(mapping['LLM_BACKEND']['allowed'])
assert defaults['llm_profiles']['openai']=={'document_text_allowed':True,'image_allowed':False}
assert defaults['llm_profiles']['ollama']==defaults['llm_profiles']['fake']==defaults['external_transfer']
assert defaults['publication']['auto_rule_ids']==[]
assert not defaults['documents']['browser_fallback_enabled'] and not defaults['documents']['hwp_preview_enabled']

api=read('contracts/openapi.json');opids=set();refcount=0
def walk(x):
 global refcount
 if isinstance(x,dict):
  if '$ref' in x:
   r=x['$ref'];refcount+=1;assert r.startswith('./domain.schema.json#/$defs/') and r.split('/')[-1] in domain['$defs']
  for v in x.values():walk(v)
 elif isinstance(x,list):
  for v in x:walk(v)
walk(api)
for path,methods in api['paths'].items():
 for method,op in methods.items():
  if method not in ['get','post','patch','delete','put']:continue
  oid=op.get('operationId')
  if oid:assert oid not in opids;opids.add(oid)
  assert set(re.findall(r'\{([^}]+)\}',path))=={p['name'] for p in op.get('parameters',[]) if p['in']=='path'}
  if method!='get':assert op['security']==[{'LocalToken':[]}]
assert api['info']['version']=='4.0'
assert api['paths']['/api/jobs/{id}/resume']['post']['responses']['409']['content']['application/json']['schema']['$ref']=='./domain.schema.json#/$defs/Error'
valid('Error',{'code':'settings_changed','message':'실행 설정이 달라 이전 작업을 재개할 수 없습니다.','retryable':False,'details':{'reason_codes':['configuration_required']}})
db=sqlite3.connect(':memory:');db.executescript((ROOT/'contracts/schema.sql').read_text())
db.execute("INSERT INTO institutions(id,name,type,district,address,operation_status,created_at,updated_at) VALUES('i','가상학교','elementary','북구','가상주소','unknown','t','t')")
db.execute("INSERT INTO seasons VALUES('s','시즌',2026,'2026-12-01','2027-03-31','test','Asia/Seoul','gwangju_5_gu',1)")
db.execute("INSERT INTO events(id,institution_id,season_id,event_key,title,event_type,created_at,updated_at) VALUES('e','i','s','main','졸업식','graduation_ceremony','t','t')")
db.execute("INSERT INTO events(id,institution_id,season_id,event_key,title,event_type,created_at,updated_at) VALUES('e2','i','s','department','학과식','degree_award_ceremony','t','t')")
def bad_sql(sql):
 try:db.execute(sql)
 except sqlite3.IntegrityError:return
 raise AssertionError('Invalid SQL state accepted')
bad_sql("INSERT INTO institutions(id,name,type,district,address,operation_status,created_at,updated_at) VALUES('b','x','unlisted','북구','x','unknown','t','t')")
db.execute("INSERT INTO manual_overrides(id,event_id,field,mode,value_json,basis,recorded_at) VALUES('o1','e','start_time','set','\"10:00\"','phone_confirmed','t')")
bad_sql("INSERT INTO manual_overrides(id,event_id,field,mode,value_json,basis,recorded_at) VALUES('o2','e','start_time','clear','null','user_recorded','t')")
db.execute("UPDATE manual_overrides SET active=0 WHERE id='o1'")
db.execute("INSERT INTO manual_overrides(id,event_id,field,mode,value_json,basis,recorded_at) VALUES('o2','e','start_time','clear','null','user_recorded','t')")
assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
assert db.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0]=='4.0'
for name in ['acquisition_json','reading_json','authority_evidence_json','recheck_json','image_size_json']:assert name in {r[1] for r in db.execute('PRAGMA table_info(documents)')}
assert 'source_document_id' in {r[1] for r in db.execute('PRAGMA table_info(manual_overrides)')}
assert 'reason_codes_json' in {r[1] for r in db.execute('PRAGMA table_info(observations)')}
db.close()
for f in list(ROOT.glob('*.md'))+[ROOT/'04_codex_handoff.txt']:
 assert not re.search(r'[\u3040-\u30ff]',f.read_text()),f
print(json.dumps({'contract_version':'4.0','schemas':len(domain['$defs']),'example_shapes_validated':len(data),'negative_shape_cases':negative_cases,'literal_quotes_present':quote_count,'reference_publication_fixtures':'consistent','reported_counts':'consistent','openapi_refs':refcount,'sql':'passed','env':{'minimal_keys':len(env),'supported_optional_keys':len(mapping),'default_fallback':'passed'},'scope':'specification checks only; no service/probe execution'},ensure_ascii=False))
