"""Reuse the original confirmation/evaluation boundary, never recount grades."""
from collections import OrderedDict
from decimal import Decimal
from course_input_adapter import adapt_legacy_result
from input_confirmation import confirm_confirmation
from graduation_service import EvaluationRequest, evaluate
from curriculum_registry import get_curriculum
from requirement_layout import category_title
from audit_details import missing_candidates, total_progress

PROGRAMS = {
 '地生（地球環境）': ('earth:earth_environment','地生','地球環境'),
 '地生（生命科學）': ('earth:life_science','地生','生命科學'),
 '物化（電子物理）': ('apc:physics','物化','電子物理'),
 '物化（應用化學）': ('apc:chemistry','物化','應用化學'),
 '資科': ('cs','資科',None),
 '數學': ('math','數學',None),
}

def audit_courses(courses, diagnostics, settings):
 if diagnostics.get('fatal') or diagnostics.get('complete') is False:
  raise ValueError('成績單有解析問題，請先核對解析事項。')
 primary = PROGRAMS.get(settings.get('primary'))
 if not primary:
  raise ValueError('請選擇主修系所。')
 if primary[0] == 'math':
  raise ValueError('數學系需另選專業領域，這個設定尚未接入；目前無法審核。')
 year = str(settings.get('handbook', ''))
 admission = str(settings.get('admission', ''))
 if year not in {'111','112','113','114','115'} or admission not in {'111','112','113','114','115'}:
  raise ValueError('請選擇有效年度。')
 primary_id = f'primary:{year}:{primary[0]}'
 if not get_curriculum(primary_id):
  raise ValueError('沒有這個版本的主修規則。')
 plan = settings.get('plan', '單主修')
 target_id = None
 target = None
 target_year = str(settings.get('targetYear', ''))
 if plan not in {'單主修','雙主修','輔系'}:
  raise ValueError('修讀規劃無效。')
 if plan != '單主修':
  target = PROGRAMS.get(settings.get('target'))
  if not target or target == primary or target_year not in {'111','112','113','114','115'}:
   raise ValueError('請核對雙主修／輔系設定。')
  slug = target[0]
  if plan == '輔系':
   slug = slug if slug.startswith('apc:') else slug.split(':')[0] + ':department'
   target_id = f'minor:{target_year}:{slug}'
  else:
   target_id = f'target:double_major:{target_year}:{slug}'
  if not get_curriculum(target_id):
   raise ValueError('沒有這個版本的目標課表。')
 adapted = adapt_legacy_result(courses)
 confirmation = confirm_confirmation(adapted.confirmation, adapted.confirmation.fingerprint)
 if not confirmation.valid or confirmation.state.value != 'CONFIRMED':
  raise ValueError('課程資料尚有無法確認的欄位，請先修正成績。')
 snapshot = evaluate(EvaluationRequest(
  admission_cohort=admission, primary_curriculum_id=primary_id,
  confirmed_course_rows=confirmation.rows, confirmed_course_fingerprint=confirmation.fingerprint,
  transcript_confirmed=True, confirmation_state='CONFIRMED', program_type=plan,
  secondary_kind={'單主修':'none','雙主修':'double_major','輔系':'minor'}[plan],
  target_curriculum_id=target_id, target_curriculum_version_candidate=target_id,
  target_curriculum_year=target_year if target else None,
  target_program=target[1] if target else None, target_track=target[2] if target else None,
 ))
 groups = OrderedDict()
 registries=[get_curriculum(primary_id)] + ([get_curriculum(target_id)] if target_id else [])
 for req in snapshot.requirements:
  if req.credits_required <= 0:
   continue
  result = snapshot.allocation.requirement_for(req.requirement_id)
  if not result:
   continue
  role = req.owner or req.role or req.curriculum_role
  label = category_title({'bucket':req.bucket,'name':req.name})
  if role in {'target','double_major','secondary'} or req.requirement_id.startswith(('target:', 'target.', 'cs.double', 'double_major.')):
   label = '雙主修' if plan == '雙主修' else '輔系'
  if role == 'minor' or req.requirement_id.startswith('minor:'):
   label = '輔系'
  group = groups.setdefault(label, {'label':label,'completed':Decimal(0),'required':Decimal(0),'missing':Decimal(0),'status':'PASS','courses':[]})
  if result.deficit > 0 or result.status == 'UNKNOWN':
   group['courses'].extend(missing_candidates(req,registries,snapshot.attempts))
  group['required'] += result.required_credits
  group['completed'] += min(result.effective_credits, result.required_credits)
  group['missing'] += result.deficit
  if result.status == 'UNKNOWN': group['status'] = 'UNKNOWN'
  elif result.status != 'PASS' and group['status'] != 'UNKNOWN': group['status'] = 'FAIL'
 def order(group):
  label = group['label']
  if label == '通識課程': return 0
  if label == '系共同必修': return 1
  if '領域必修' in label: return 2
  if '領域選修' in label: return 3
  return {'系內其他選修':4,'自由選修':5,'雙主修':7,'輔系':7}.get(label,6)
 return {'rows':[{k:float(v) if isinstance(v,Decimal) else v for k,v in g.items()} for g in sorted(groups.values(),key=order)],
         'total':total_progress(snapshot), 'secondaryTotal':total_progress(snapshot, secondary=True) if target_id else None,
         'secondaryLabel':plan if target_id else None,
         'snapshot':snapshot.snapshot_id, 'primary':primary_id, 'target':target_id,
         'note':'各類分開核對，不相加作為總畢業學分；待確認不等於通過。'}
