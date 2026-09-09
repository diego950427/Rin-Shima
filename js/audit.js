// Lieflat Charts F5 Tick Rows, basics-gallery.html / "Six teams, shipped and counted".
// Preserve the queue, guide line, variable-height ticks and fifth-unit dots.
// One tick = one credit. Partial credit uses a partial-height dark tick.
const auditPanel = document.querySelector('#audit-panel');
const auditStatus = document.querySelector('#audit-status');
const auditChart = document.querySelector('#audit-chart');
const confirmButton = document.querySelector('#confirm-audit');
let uploadedFile = null;
let auditRequest = null;
let chartObserver = null;
document.querySelector('#transcript-file').addEventListener('change', () => {
 resetAudit(); uploadedFile=null; auditPanel.hidden=true;
});
function resetAudit() {
 auditRequest?.abort(); auditRequest=null;
 chartObserver?.disconnect(); chartObserver=null;
 auditChart.replaceChildren(); auditStatus.textContent='';
 confirmButton.disabled=false;
}
function svgNode(tag, attrs, text) {
 const node=document.createElementNS('http://www.w3.org/2000/svg',tag);
 Object.entries(attrs).forEach(([key,value])=>node.setAttribute(key,String(value)));
 if(text !== undefined) node.textContent=text;
 return node;
}
function drawAudit(data) {
 const ink='#292929', grid='#c6c5bf', width=760, x0=240, px=7;
 const max=Math.max(...data.rows.map(r=>r.required),1);
 const step=Math.min(px,300/max);
 const scroll=document.createElement('div'); scroll.className='audit-chart-scroll'; scroll.tabIndex=0;
 data.rows.forEach((row,i)=>{
  const svg=svgNode('svg',{viewBox:`0 0 ${width} 66`,role:'group','aria-label':row.label});
  const y=24;
  svg.append(svgNode('text',{x:224,y:y+3,'text-anchor':'end','font-size':16,fill:ink},row.label));
  svg.append(svgNode('line',{x1:x0,y1:y+9,x2:x0+max*step,y2:y+9,stroke:grid,'stroke-width':.6}));
  for(let k=0;k<Math.ceil(row.required);k++) {
   const x=x0+k*step+step/2, h=9+((Math.sin((k+1)*127.1+(i+2)*311.7)*43758.5453)%1+1)%1*6;
   svg.append(svgNode('line',{x1:x,y1:y+9,x2:x,y2:y+9-h,stroke:grid,'stroke-width':1}));
   const fill=Math.max(0,Math.min(1,row.completed-k));
   if(fill>0)svg.append(svgNode('line',{x1:x,y1:y+9,x2:x,y2:y+9-h*fill,stroke:ink,'stroke-width':1.5,class:'audit-tick',style:`animation-delay:${i*.08+k*.012}s`}));
   if(k%5===4) svg.append(svgNode('circle',{cx:x,cy:y+14,r:.8,fill:grid}));
  }
  svg.append(svgNode('line',{x1:x0+row.required*step,y1:y-10,x2:x0+row.required*step,y2:y+17,stroke:ink,'stroke-width':1}));
  svg.append(svgNode('text',{x:570,y:y+3,'font-size':17,fill:ink},`${row.completed} / ${row.required} 學分`));
  const status=row.status==='UNKNOWN'?'待確認':row.missing>0?`尚缺 ${row.missing} 學分`:'學分已達標';
  const trigger=svgNode('text',{x:570,y:y+25,'font-size':14,fill:ink,tabindex:0,role:'button','aria-expanded':'false',class:'deficit-trigger'},status);
  svg.append(trigger); scroll.append(svg);
  const details=document.createElement('div'); details.className='missing-window'; details.hidden=true;
  const title=document.createElement('h4');title.textContent=row.label;details.append(title);
  const courses=row.courses||[];
  if(!courses.length){const p=document.createElement('p');p.textContent=row.missing>0?'此類以學分門檻計算，目前沒有可列出的指定缺課。':'目前沒有指定缺課；其他條件以審核狀態為準。';details.append(p);}
  const seen=new Set();
  courses.forEach(course=>{
   if(seen.has(course.name))return;seen.add(course.name);
   const item=document.createElement('section');const name=document.createElement('h5');name.textContent=course.name;item.append(name);
   if(course.titleOnly){details.append(item);return;}
   const mode=document.createElement('p');mode.textContent=course.mode;item.append(mode);
   if(!course.offerings.length){const p=document.createElement('p');p.textContent='115 上學期未查到開課。年級、下學期、時間／教室及課碼尚無資料。';item.append(p);}
   course.offerings.forEach(o=>{
    const p=document.createElement('p');p.textContent=`${o.class} · ${o.semester}\n${o.timeRoom}\n課程代碼：${o.code} · ${o.department}`;item.append(p);
   });details.append(item);
  });
  scroll.append(details);
  const toggle=()=>{details.hidden=!details.hidden;trigger.setAttribute('aria-expanded',String(!details.hidden));};
  trigger.addEventListener('click',toggle);trigger.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle();}});
 });
 auditChart.append(scroll);
 chartObserver=new IntersectionObserver(entries=>{entries.forEach(e=>{if(e.isIntersecting){e.target.classList.add('is-visible');chartObserver.unobserve(e.target);}});},{threshold:.15});
 scroll.querySelectorAll('svg').forEach(svg=>chartObserver.observe(svg));
 drawConvergence(data.total, auditChart, chartObserver, svgNode, data.secondaryTotal, data.secondaryLabel);
}

// F4 Tick Donut: 100 radial tick positions, grayscale sectors, central value.
function drawTotal(total) {
 const box=document.createElement('section');box.className='total-ring';
 const title=document.createElement('h3');title.textContent='總畢業學分';box.append(title);
 if(!total?.available){const p=document.createElement('p');p.textContent='總學分待確認';box.append(p);auditChart.append(box);return;}
 const ratio=Math.max(0,Math.min(1,total.completed/total.required));
 const svg=svgNode('svg',{viewBox:'0 0 400 370',role:'group','aria-label':`已採計 ${total.completed}，要求 ${total.required} 學分`});
 const center=svgNode('text',{x:200,y:177,'text-anchor':'middle','font-size':29},`${total.completed} / ${total.required}`);
 const caption=svgNode('text',{x:200,y:207,'text-anchor':'middle','font-size':16},'已採計學分');
 const groups=[svgNode('g',{tabindex:0,role:'button','aria-label':`已採計 ${total.completed} 學分`}),svgNode('g',{tabindex:0,role:'button','aria-label':`尚缺 ${Math.max(0,total.required-total.completed)} 學分`})];
 for(let k=0;k<100;k++) {
  const angle=(k*3.6-90)*Math.PI/180, radius=112, len=12+(k*7%6);
  const fill=Math.max(0,Math.min(1,ratio*100-k));
  [[0,fill,'#434343'],[fill,1,'#b0afa9']].forEach(([start,end,color],idx)=>{
   if(end<=start)return;
   const a=radius+len*start,b=radius+len*end;
   const line=svgNode('line',{x1:200+a*Math.cos(angle),y1:180+a*Math.sin(angle),x2:200+b*Math.cos(angle),y2:180+b*Math.sin(angle),stroke:color,'stroke-width':2.5,class:'audit-tick',style:`animation-delay:${k*.012}s`});
   groups[idx].append(line);
  });
 }
 groups.forEach((g,index)=>{
  const show=()=>{center.textContent=String(index?Math.max(0,total.required-total.completed):total.completed);caption.textContent=index?'尚缺學分':'已採計學分';};
  g.addEventListener('mouseenter',show);g.addEventListener('focus',show);g.addEventListener('click',show);
  g.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();show();}});
  svg.append(g);
 });
 svg.append(center,caption);box.append(svg);
 const legend=document.createElement('div');legend.className='ring-legend';
 ['已採計','尚缺'].forEach((label,index)=>{const button=document.createElement('button');button.type='button';button.textContent=`${label} ${index?Math.max(0,total.required-total.completed):total.completed} 學分`;button.addEventListener('click',()=>groups[index].dispatchEvent(new Event('focus')));legend.append(button);});
 box.append(legend);auditChart.append(box);chartObserver.observe(svg);
}
confirmButton.addEventListener('click',async()=>{
 if(!appliedSettings){auditStatus.textContent='請先套用主修設定。';return;}
 if(!uploadedFile){auditStatus.textContent='請先匯入成績。';return;}
 resetAudit(); const current=new AbortController();auditRequest=current;confirmButton.disabled=true;
 auditStatus.textContent='正在檢核…';
 try {
  const response=await (window.utFetch || fetch)('/api/audit?settings='+encodeURIComponent(JSON.stringify(appliedSettings)),{
   method:'POST',body:uploadedFile,headers:{'Content-Type':'application/pdf','X-Confirm-Courses':'yes'},signal:current.signal});
  if(!response.headers.get('content-type')?.includes('application/json'))throw Error('審核服務未回應，請稍後重試。');
  const data=await response.json(); if(!response.ok)throw Error(data.error||'審核失敗。');
  drawAudit(data);auditStatus.textContent='';
 }catch(error){if(error.name!=='AbortError')auditStatus.textContent=error instanceof TypeError?'審核服務無法連線，請重試。':error.message;}
 finally{if(auditRequest===current){auditRequest=null;confirmButton.disabled=false;}}
});
