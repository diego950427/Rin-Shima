// Lieflat L5 Radial Convergence / lupi-gallery.html, actual allocation records.
function drawConvergence(total, container, observer, el, secondaryTotal=null, secondaryLabel='雙主修') {
 if(secondaryTotal){
  const wrapper=document.createElement('section');wrapper.className='program-progress';
  const switcher=document.createElement('div');switcher.className='program-switch';
  const input=document.createElement('input');input.type='checkbox';input.id='progress-program-switch';input.setAttribute('role','switch');input.setAttribute('aria-label',`切換主修／${secondaryLabel}`);
  const label=document.createElement('label');label.htmlFor=input.id;
  const words=document.createElement('span');words.className='program-switch-words';words.setAttribute('aria-hidden','true');
  ['主修',secondaryLabel].forEach((text,i)=>{const span=document.createElement('span');span.className=i?'checked':'unchecked';span.textContent=text;words.append(span);});
  label.append(words);switcher.append(input,label);
  const panels=document.createElement('div');panels.className='program-progress-panels';
  const primary=document.createElement('div'),secondary=document.createElement('div');
  primary.setAttribute('aria-label','主修學分');secondary.setAttribute('aria-label',`${secondaryLabel}學分`);
  secondary.hidden=true;panels.append(primary,secondary);wrapper.append(switcher,panels);container.append(wrapper);
  drawConvergence({...total,title:'主修學分'},primary,observer,el);
  drawConvergence({...secondaryTotal,title:`${secondaryLabel}學分`},secondary,observer,el);
  input.addEventListener('change',()=>{
   primary.hidden=input.checked;secondary.hidden=!input.checked;
   const active=input.checked?secondary:primary;
   if(!matchMedia('(prefers-reduced-motion: reduce)').matches)active.animate([{opacity:.35},{opacity:1}],{duration:240,easing:'ease-out'});
  });
  return;
 }
 const box=document.createElement('section');box.className='total-ring convergence';
 const title=document.createElement('h3');title.textContent=total?.title||'主修學分';box.append(title);
 const value=document.createElement('p');value.className='convergence-total';value.textContent=total?.available?`${total.completed} / ${total.required} 學分`:'總學分待確認';box.append(value);
 if(!total?.mapping_valid || !total?.partition_valid){const note=document.createElement('p');note.textContent='總學分分類尚待確認，暫不補畫缺失線。';box.append(note);container.append(box);return;}
 const all=[...total.records,...total.gaps];
 const categories=[...new Set(all.map(r=>r.category))];
 const records=all.sort((a,b)=>categories.indexOf(a.category)-categories.indexOf(b.category));
 const CX=460,CY=380,R=225;
 const pol=(radius,deg)=>[CX+radius*Math.cos(deg*Math.PI/180),CY+radius*Math.sin(deg*Math.PI/180)];
 const svg=el('svg',{viewBox:'0 0 920 760',role:'group','aria-label':'深灰線為採計課程，淡紅線每條代表一待補學分（非指定課程）'});
 const readout=document.createElement('p');readout.className='convergence-readout';readout.setAttribute('aria-live','polite');readout.textContent='點選課程或分類';
 const paths=[];const hubs=[];
 const detail=document.createElement('div');detail.className='convergence-detail';detail.hidden=true;
 const detailSlot=document.createElement('div');detailSlot.className='convergence-detail-slot';detailSlot.append(detail);
 let currentCategory=null;
 let offset=0;
 categories.forEach((category,h)=>{
  const members=records.filter(r=>r.category===category);
  const angle=(offset+members.length/2)/records.length*360-90;
  const start=(offset-.5)/records.length*360-90;
  const end=(offset+members.length-.5)/records.length*360-90;
  const [hx,hy]=pol(64,angle);hubs.push({hx,hy,angle,start,end,category,members});offset+=members.length;
 });
 const highlight=(category,text,selected=null)=>{
  paths.forEach(p=>{
   const active=p===selected;
   p.style.opacity=active?'1':p.dataset.category===category?'.48':'.06';
   p.style.strokeWidth=active?'2':'1';
   p.style.stroke=p.classList.contains('is-missing')?(active?'#bd7777':'#d9a2a2'):(active?'#434343':'#a8a7a0');
  });
  readout.textContent=text;
  if(currentCategory===category)return;currentCategory=category;
  detail.hidden=false;detail.replaceChildren();
  detail.scrollTop=0;
  const heading=document.createElement('h4');heading.textContent=category;detail.append(heading);
  const members=records.filter(r=>r.category===category);
  members.filter(r=>!r.missing).forEach(r=>{
   const line=document.createElement('div');line.className='course-route';
   const name=document.createElement('span');name.textContent=r.name;
   const route=document.createElement('span');route.className='route-connector';route.setAttribute('aria-hidden','true');
   const end=document.createElement('span');end.textContent=`${r.credits} 學分 → ${category}`;
   line.append(name,route,end);detail.append(line);
  });
  const gap=members.filter(r=>r.missing).reduce((sum,r)=>sum+r.credits,0);
  if(gap){const line=document.createElement('div');line.className='course-route missing-route';line.textContent=`待補 ${Number(gap.toFixed(4))} 學分 → ${category}（非指定課程）`;detail.append(line);}
  if(!matchMedia('(prefers-reduced-motion: reduce)').matches)detail.animate([{opacity:0,transform:'translateY(-8px)'},{opacity:1,transform:'translateY(0)'}],{duration:280,easing:'ease-out'});
 };
 const selectCategory=hub=>{
  const done=hub.members.filter(r=>!r.missing).reduce((s,r)=>s+r.credits,0);
  const required=hub.members.reduce((s,r)=>s+r.credits,0);
  highlight(hub.category,`${hub.category} · 已採計 ${done}／要求 ${required} 學分`);
 };
 // Broad sector targets sit beneath route targets: blank space selects a group.
 hubs.forEach(hub=>{
  const [sx,sy]=pol(R+105,hub.start),[ex,ey]=pol(R+105,hub.end);
  const region=el('path',{d:`M${CX} ${CY} L${sx} ${sy} A${R+105} ${R+105} 0 ${hub.end-hub.start>180?1:0} 1 ${ex} ${ey} Z`,fill:'transparent',class:'convergence-region'});
  region.dataset.category=hub.category;
  region.addEventListener('pointerenter',()=>selectCategory(hub));
  region.addEventListener('click',()=>selectCategory(hub));
  svg.append(region);
 });
 const routeLayer=el('g',{'pointer-events':'none'}),hitLayer=el('g',{}),nodeLayer=el('g',{});
 svg.append(routeLayer,hitLayer,nodeLayer);
 records.forEach((record,i)=>{
  const deg=i/records.length*360-90,[px,py]=pol(R,deg), hub=hubs[categories.indexOf(record.category)];
  const path=el('path',{d:`M${px} ${py} C${CX+(px-CX)*.42} ${CY+(py-CY)*.42} ${CX+(hub.hx-CX)*.3} ${CY+(hub.hy-CY)*.3} ${hub.hx} ${hub.hy}`,fill:'none',stroke:record.missing?'#d9a2a2':'#a8a7a0','stroke-width':1,opacity:.7,pathLength:1,class:`convergence-path${record.missing?' is-missing':''}`,style:`animation-delay:${i*.012}s`});
  path.dataset.credits=record.credits;
  path.dataset.category=record.category;paths.push(path);routeLayer.append(path);
  const dot=el('circle',{cx:px,cy:py,r:7,fill:'transparent',tabindex:0,role:'button','aria-label':`${record.name}，${record.credits} 學分，${record.category}`});
  const select=()=>highlight(record.category,`${record.name} · ${record.credits} 學分 · ${record.category}`,path);
  const hit=el('path',{d:path.getAttribute('d'),fill:'none',stroke:'transparent','stroke-width':10,'pointer-events':'stroke',class:'convergence-hit','aria-hidden':'true'});
  hit.addEventListener('pointerenter',select);hit.addEventListener('click',select);
  hit.addEventListener('pointerleave',()=>selectCategory(hub));hitLayer.append(hit);
  dot.addEventListener('mouseenter',select);dot.addEventListener('focus',select);dot.addEventListener('click',select);
  dot.addEventListener('mouseleave',()=>selectCategory(hub));
  dot.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select();}});
  nodeLayer.append(el('circle',{cx:px,cy:py,r:2.1,fill:record.missing?'#d9a2a2':'#6a6963','pointer-events':'none'}),dot);
  const [lx,ly]=pol(R+12,deg),flip=deg>90&&deg<270;
  nodeLayer.append(el('text',{x:lx,y:ly,'font-size':10,fill:'#6a6963','pointer-events':'none','text-anchor':flip?'end':'start','dominant-baseline':'middle',transform:`rotate(${flip?deg+180:deg} ${lx} ${ly})`},`${record.missing?'G':'C'}-${String(i+1).padStart(2,'0')}`));
 });
 hubs.forEach(hub=>{
  const credits=hub.members.reduce((sum,r)=>sum+r.credits,0);
  const side=Math.abs(Math.cos(hub.angle*Math.PI/180))>.75;
  const [tx,ty]=pol(side?R+58:R+116,hub.angle),[gx,gy]=pol(side?R+42:R+90,hub.angle);
  svg.append(el('line',{x1:hub.hx,y1:hub.hy,x2:gx,y2:gy,stroke:'#c6c5bf','stroke-width':.7,'stroke-dasharray':'1 3','pointer-events':'none'}));
  const hubNode=el('circle',{cx:hub.hx,cy:hub.hy,r:Math.sqrt(hub.members.length)*3.5,fill:'#292929'});svg.append(hubNode);
  const done=hub.members.filter(r=>!r.missing).reduce((sum,r)=>sum+r.credits,0);
  const label=el('text',{x:tx,y:ty,'font-size':16,'font-weight':700,fill:'#292929','text-anchor':side?(tx>CX?'start':'end'):'middle',tabindex:0,role:'button',class:'convergence-label','aria-label':`${hub.category} · ${done} / ${credits}`});
  label.append(el('tspan',{x:tx,dy:0},side?hub.category:`${hub.category} · ${done} / ${credits}`));
  if(side)label.append(el('tspan',{x:tx,dy:24},`${done} / ${credits}`));
  const select=()=>highlight(hub.category,`${hub.category} · 已採計 ${done}／要求 ${credits} 學分`);
  [hubNode,label].forEach(node=>{node.addEventListener('mouseenter',select);node.addEventListener('click',select);node.addEventListener('focus',select);});
  label.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();select();}});svg.append(label);
 });
 box.append(svg,readout,detailSlot);container.append(box);observer.observe(svg);
}
