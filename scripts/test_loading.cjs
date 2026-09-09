const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const code=fs.readFileSync(require('node:path').join(__dirname,'../js/loading.js'),'utf8');
async function scenario(timeout=false){
 let removed=false,loaded,fontsDone;
 const timers=new Map(),handlers={},classes=new Set(),sibling={tagName:'MAIN',inert:false};
 const loader={parentElement:{children:[]},classList:{add:n=>classes.add(n)},addEventListener:(k,f)=>handlers[k]=f,remove:()=>removed=true};
 loader.parentElement.children=[loader,sibling];
 const context={document:{querySelector:()=>loader,readyState:'loading',fonts:{ready:new Promise(r=>fontsDone=r)}},
 addEventListener:(name,f)=>{if(name==='load')loaded=f;},setTimeout:(f,ms)=>{timers.set(ms,f);return ms;},clearTimeout:ms=>timers.delete(ms),Promise};
 vm.runInNewContext(code,context);
 assert(sibling.inert);assert(!classes.has('is-ready'));
 if(timeout)timers.get(12000)();else{loaded();fontsDone();await new Promise(r=>setImmediate(r));}
 assert(classes.has('is-ready'));assert(!sibling.inert);
 timers.get(400)();assert(removed);
}
(async()=>{await scenario();await scenario(true);console.log('PASS loading gate, ready fade, timeout fallback and interaction restored');})();
