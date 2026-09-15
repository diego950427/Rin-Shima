const lookupMatchesGrade=(course,grade)=>{
 if(!grade)return true;
 if(grade==='ge')return /通識/.test(course.class_name)&&course.official_category!=='共同選修';
 if(grade==='common')return course.official_category==='共同選修';
 const classes=[course.class_name,course.mixed_classes].join(',').split(/[,，、\n]/);
 if(grade==='graduate')return classes.some(name=>/[碩博]/.test(name));
 if(/^[1-4]$/.test(grade))return classes.some(name=>!/[碩博]/.test(name)&&new RegExp('[系組班]'+['','一','二','三','四'][+grade]).test(name));
 return false;
};
if(typeof module!=='undefined')module.exports={lookupMatchesGrade};
if(typeof document!=='undefined') (() => {
 const form=document.querySelector('#lookup-form'),status=document.querySelector('#lookup-status'),results=document.querySelector('#lookup-results');
 const fields=Object.fromEntries(['keyword','department','grade','day','period'].map(k=>[k,document.querySelector('#lookup-'+k)]));
 for(let i=1;i<=14;i++)fields.period.add(new Option(`第 ${i} 節`,String(i)));
 let courses=null,loading=null,page=0,filtered=[],currentTerm='115_1',loadVersion=0;
 const pageSize=20;
 const normalize=s=>String(s||'').normalize('NFKC').toLowerCase().trim();
 const slots=raw=>[...normalize(raw).matchAll(/\(([一二三四五六日])\)\s*(\d+)(?:\s*[-~～]\s*(\d+))?/g)].map(m=>({day:m[1],first:+m[2],last:+(m[3]||m[2])}));
 const text=(tag,value)=>{const n=document.createElement(tag);n.textContent=value;return n;};
 function render(){
  const term=normalize(fields.keyword.value),dept=fields.department.value,grade=fields.grade.value,day=fields.day.value,period=+fields.period.value;
  filtered=courses.filter(c=>{
   if(term&&!c.search.includes(term))return false;
   if(dept&&!c.departments.includes(dept))return false;
   if(!lookupMatchesGrade(c,grade))return false;
   return (!day&&!period)||c.slots.some(s=>(!day||s.day===day)&&(!period||(period>=s.first&&period<=s.last)));
  });
  const pages=Math.max(1,Math.ceil(filtered.length/pageSize));page=Math.min(page,pages-1);
  status.textContent=`${filtered.length} 門課`;
  results.replaceChildren();
  const selected=[term&&`搜尋：${fields.keyword.value}`,dept,grade&&fields.grade.selectedOptions[0].textContent,day&&`星期${day}`,period&&`第 ${period} 節`].filter(Boolean);
  if(selected.length)results.append(text('p',selected.join(' · ')));
  if(!filtered.length){
   results.append(text('p','目前資料沒有同時符合以上條件的課程。'));
   if(period){
    const remove=text('button','');remove.type='button';remove.className='crosshair-button';
    remove.append(text('span','不限節次'));
    remove.addEventListener('click',()=>{fields.period.value='';page=0;render();});
    results.append(remove);
   }
  }
  filtered.slice(page*pageSize,(page+1)*pageSize).forEach(c=>{
   const article=text('article','');article.className='lookup-course';
   article.append(text('h3',c.course_name_zh),text('p',`${c.course_code} · ${c.credits} 學分 · ${c.required_elective}`),text('p',`${c.class_name} · ${c.departments.join('、')}`),text('p',c.teaching_raw||'時間與教室未提供'));
   if(c.mixed_classes||c.remarks){const details=text('details','');details.append(text('summary','合班與備註'));if(c.mixed_classes)details.append(text('p',c.mixed_classes));if(c.remarks)details.append(text('p',c.remarks));article.append(details);}
   results.append(article);
  });
  document.querySelector('.lookup-pagination').hidden=pages<=1;
  document.querySelector('#lookup-page').textContent=`${page+1} / ${pages}`;
  document.querySelector('#lookup-prev').disabled=page===0;
  document.querySelector('#lookup-next').disabled=page===pages-1;
 }
 window.loadCourseLookup=(term=currentTerm)=>{
  if(typeof term!=='string')term=currentTerm;
  if(!['115_1','114_2','114_1'].includes(term))return;
  if(term===currentTerm&&(courses||loading))return;
  currentTerm=term;
  const version=++loadVersion;
  courses=null;page=0;results.replaceChildren();
  document.querySelector('.lookup-pagination').hidden=true;
  const oldDepartment=fields.department.value;
  status.textContent='正在載入開課資料…';
  document.querySelector('.lookup-source').textContent=`${term.split('_')[0]} ${term.endsWith('_1')?'上':'下'}學期 · 載入中`;
  loading=fetch(`assets/semester_courses_${term}.json`,{signal:AbortSignal.timeout(15000)}).then(r=>{if(!r.ok)throw Error();return r.json();}).then(data=>{
   if(version!==loadVersion)return;
   if(`${data.query.academic_year}_${data.query.semester}`!==term||data.query.campus!=='博愛')throw Error('term mismatch');
   courses=data.courses.map(c=>({...c,search:normalize([c.course_name_zh,c.course_code,c.teaching_raw,c.class_name,...c.departments].join(' ')),slots:slots(c.teaching_raw)}));
   fields.department.replaceChildren(new Option('全部',''));
   [...new Set(courses.flatMap(c=>c.departments))].sort((a,b)=>a.localeCompare(b,'zh-Hant')).forEach(d=>fields.department.add(new Option(d,d)));
   if([...fields.department.options].some(option=>option.value===oldDepartment))fields.department.value=oldDepartment;
   const date=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Taipei',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(data.captured_at)).replaceAll('-','/');
   document.querySelector('.lookup-source').textContent=`${data.query.academic_year} ${+data.query.semester===1?'上':'下'}學期 · ${data.query.campus}校區 · ${date} 資料`;
   document.querySelectorAll('[data-lookup-term]').forEach(button=>button.setAttribute('aria-current',String(button.dataset.lookupTerm===term)));
   render();
  }).catch(()=>{
   if(version!==loadVersion)return;
   courses=null;
   document.querySelector('.lookup-source').textContent=`${term.split('_')[0]} ${term.endsWith('_1')?'上':'下'}學期 · 載入失敗`;
   status.textContent='開課資料未能載入，請重新選擇學期再試。';
  }).finally(()=>{if(version===loadVersion)loading=null;});
 };
 document.querySelectorAll('[data-lookup-term]').forEach(button=>button.addEventListener('click',()=>{
  button.closest('details').open=false;
  window.loadCourseLookup(button.dataset.lookupTerm);
 }));
 let timer;
 form.addEventListener('submit',e=>e.preventDefault());
 form.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>{page=0;if(courses)render();},120);});
 form.addEventListener('change',()=>{clearTimeout(timer);page=0;if(courses)render();});
 form.addEventListener('reset',()=>setTimeout(()=>{page=0;if(courses)render();},0));
 [['prev',-1],['next',1]].forEach(([name,direction])=>document.querySelector('#lookup-'+name).addEventListener('click',()=>{page+=direction;render();status.scrollIntoView({block:'start',behavior:'instant'});}));
})();
