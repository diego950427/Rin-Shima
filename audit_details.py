"""Read-only course suggestions; never substitutes offerings for credit evidence."""
import json
from pathlib import Path
from handbook_rules import normalize_course_name
from snapshot_projection import statistics_projection_from_payload, build_chart_datasets
from requirement_layout import category_title

CATALOG = json.loads((Path(__file__).parent/'core/data/semester_courses_115_1.json').read_text(encoding='utf-8'))

def missing_candidates(req, registries, attempts):
 if req.bucket.startswith('ge_') or req.bucket == 'university_compulsory':
  labels={'ge_common_elective':'共同選修','university_compulsory':'共同必修'}
  return [{'name':labels.get(req.bucket,req.bucket.removeprefix('ge_')),'titleOnly':True,'mode':'','offerings':[]}]
 names = [(name, None) for name in req.eligible_course_names]
 mode = '指定課程' if names else '可補選課程（不是全部必修）'
 if not names:
  for record in registries:
   for pool_id in req.eligible_pool_ids:
    pool = record.get('course_pools', {}).get(pool_id, {})
    for course in pool.get('courses', ()):
     names.append((course.get('name') or course.get('raw_title'), course.get('credits')))
 seen=set(); items=[]
 for name, credits in names:
  if not name: continue
  key=normalize_course_name(name)
  if key in seen: continue
  seen.add(key)
  if any(token in key for token in ('專業實習','專題研究','資訊專題')):
   items.append({'name':name,'titleOnly':True,'mode':'','offerings':[]})
   continue
  # Completed exact-name/credit attempts are not displayed as unstudied courses.
  if any(normalize_course_name(a.course_name)==key and a.earned_credits>0 and
         (credits is None or float(a.credits)==float(credits)) for a in attempts):
   continue
  matches=[c for c in CATALOG['courses'] if normalize_course_name(c.get('course_name_zh',''))==key
           and (credits is None or str(c.get('credits','')) in (str(credits),str(int(float(credits)))))]
  offerings=[{'class':c.get('class_name') or '未提供年級',
              'semester':'115 學年上學期', 'timeRoom':c.get('teaching_raw') or '未提供',
              'code':c.get('course_code') or '未提供',
              'department':'、'.join(c.get('departments',[]))} for c in matches]
  items.append({'name':name,'mode':mode,'offerings':offerings})
 return items

def total_progress(snapshot, secondary=False):
 stats=statistics_projection_from_payload(snapshot.as_dict())
 total=dict(build_chart_datasets(stats, snapshot_id=snapshot.snapshot_id)['f11'])
 def selected(req):
  role=(req.owner or req.role or req.curriculum_role).lower()
  return role in {'target','secondary','double_major','minor'} if secondary else role=='primary'
 def label_for(req):
  if secondary:
   return {'required':'指定必修','other_required':'其他開設課程','elective':'選修課程'}.get(req.bucket,req.name)
  return category_title({'bucket':req.bucket,'name':req.name})
 if secondary:
  results=[snapshot.allocation.requirement_for(r.requirement_id) for r in snapshot.requirements if selected(r) and r.credits_required>0]
  results=[r for r in results if r]
  total={'available':bool(results),
         'completed':sum(float(min(r.effective_credits,r.required_credits)) for r in results),
         'required':sum(float(r.required_credits) for r in results)}
 if total.get('available'):
  total['completed']=float(total['completed']); total['required']=float(total['required'])
 requirements={r.requirement_id:r for r in snapshot.requirements}
 attempts={a.attempt_id:a for a in snapshot.attempts}
 records={}
 for allocation in snapshot.allocation.allocations:
  for portion in allocation.portions:
   req=requirements.get(portion.requirement_id)
   kinds={'EXCLUSIVE','SHARED_SHADOW'} if secondary else {'EXCLUSIVE'}
   if not req or portion.allocation_kind not in kinds or portion.credits<=0: continue
   if not selected(req): continue
   label=label_for(req)
   key=(portion.attempt_id,label)
   record=records.setdefault(key,{'name':attempts[portion.attempt_id].course_name,'category':label,'credits':0})
   record['credits']+=float(portion.credits)
 total['records']=list(records.values())
 total['mapping_valid']=abs(sum(r['credits'] for r in records.values())-float(total.get('completed') or 0))<.00001
 # Only partition the remaining total when category requirements form the
 # exact primary graduation denominator. Never force overlapping gates to fit.
 targets={}
 for req in snapshot.requirements:
  if selected(req) and req.credits_required>0:
   label=label_for(req)
   targets[label]=targets.get(label,0)+float(req.credits_required)
 gaps=[]
 for category, required in targets.items():
  completed=sum(r['credits'] for r in records.values() if r['category']==category)
  gap=required-completed
  if gap>0:
   # Each gap edge is one credit (last edge may be fractional), not a course.
   while gap>0.000001:
    value=min(1.0,gap)
    gaps.append({'name':'待補學分（非指定課程）','category':category,'credits':value,'missing':True})
    gap-=value
 total['partition_valid']=bool(total.get('available') and total['mapping_valid'] and
     abs(sum(targets.values())-total['required'])<.00001 and
     abs(sum(r['credits'] for r in records.values())+sum(r['credits'] for r in gaps)-total['required'])<.00001)
 total['gaps']=gaps if total['partition_valid'] else []
 return total
