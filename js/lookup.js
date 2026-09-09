(() => {
 const form=document.querySelector('#lookup-form'),status=document.querySelector('#lookup-status'),results=document.querySelector('#lookup-results');
 const fields=Object.fromEntries(['keyword','department','grade','day','period'].map(k=>[k,document.querySelector('#lookup-'+k)]));
 for(let i=1;i<=14;i++)fields.period.add(new Option(String(i),String(i)));
 let courses=null,loading=null,page=0,filtered=[];
 const pageSize=20;
 const normalize=s=>String(s||'').normalize('NFKC').toLowerCase().trim();
 const slots=raw=>[...normalize(raw).matchAll(/\(([一二三四五六日])\)\s*(\d+)(?:\s*[-~～]\s*(\d+))?/g)].map(m=>({day:m[1],first:+m[2],last:+(m[3]||m[2])}));
 const text=(tag,value)=>{const n=document.createElement(tag);n.textContent=value;return n;};
 function render(){
  const term=normalize(fields.keyword.value),dept=fields.department.value,grade=fields.grade.value,day=fields.day.value,period=+fields.period.value;
  filtered=courses.filter(c=>{
   if(term&&!c.search.includes(term))return false;
   if(dept&&!c.departments.includes(dept))return false;
   if(grade==='ge'&&(!/通識/.test(c.class_name)||c.official_category==='共同選修'))return false;
   if(grade==='common'&&c.official_category!=='共同選修')return false;
   if(/^[1-4]$/.test(grade)&&!new RegExp('[系組班碩博]'+['','一','二','三','四'][+grade]).test(c.class_name+','+c.mixed_classes))return false;
   return (!day&&!period)||c.slots.some(s=>(!day||s.day===day)&&(!period||(period>=s.first&&period<=s.last)));
  });
  const pages=Math.max(1,Math.ceil(filtered.length/pageSize));page=Math.min(page,pages-1);
  status.textContent=`${filtered.length} 門課`;
  results.replaceChildren();
  if(!filtered.length)results.append(text('p','沒有符合條件的課程，請調整篩選。'));
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
 window.loadCourseLookup=()=>{
  if(courses||loading)return;
  status.textContent='正在載入開課資料…';
  loading=fetch('assets/semester_courses_115_1.json').then(r=>{if(!r.ok)throw Error();return r.json();}).then(data=>{
   courses=data.courses.map(c=>({...c,search:normalize([c.course_name_zh,c.course_code,c.teaching_raw,c.class_name,...c.departments].join(' ')),slots:slots(c.teaching_raw)}));
   [...new Set(courses.flatMap(c=>c.departments))].sort((a,b)=>a.localeCompare(b,'zh-Hant')).forEach(d=>fields.department.add(new Option(d,d)));
   render();
  }).catch(()=>{status.textContent='開課資料未能載入，請從右上角重新切換查課再試。';}).finally(()=>loading=null);
 };
 let timer;
 form.addEventListener('submit',e=>e.preventDefault());
 form.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>{page=0;if(courses)render();},120);});
 form.addEventListener('change',()=>{clearTimeout(timer);page=0;if(courses)render();});
 form.addEventListener('reset',()=>setTimeout(()=>{page=0;if(courses)render();},0));
 [['prev',-1],['next',1]].forEach(([name,direction])=>document.querySelector('#lookup-'+name).addEventListener('click',()=>{page+=direction;render();status.scrollIntoView({block:'start',behavior:'instant'});}));
})();
