// Streamlit Components v1 bridge. UI state stays in this iframe across reruns.
(() => {
 const pending=new Map();
 let acknowledged=null;
 const send=(type,body={})=>parent.postMessage({isStreamlitMessage:true,type,...body},'*');
 const value=payload=>send('streamlit:setComponentValue',{value:payload,dataType:'json'});
 addEventListener('message',event=>{
  if(event.source!==parent || event.data?.type!=='streamlit:render')return;
  const response=event.data.args?.response;
  if(!response || response.id===acknowledged)return;
  acknowledged=response.id;
  const item=pending.get(response.id);
  if(item){pending.delete(response.id);item.cleanup();item.resolve(new Response(JSON.stringify(response.data),{status:response.status,headers:{'Content-Type':'application/json'}}));}
  value({kind:'ack',id:response.id});
 });
 window.utFetch=async(url,options)=>{
  if(options.signal?.aborted)throw new DOMException('Aborted','AbortError');
  const operation=url.startsWith('/api/audit')?'audit':'transcript';
  const pdf=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=()=>reject(Error('無法讀取檔案。'));reader.readAsDataURL(options.body);});
  if(options.signal?.aborted)throw new DOMException('Aborted','AbortError');
  return new Promise((resolve,reject)=>{
   const id=crypto.randomUUID();
   const abort=()=>{pending.delete(id);cleanup();reject(new DOMException('Aborted','AbortError'));};
   const timer=setTimeout(()=>{pending.delete(id);cleanup();reject(Error('雲端服務回應逾時，請重試。'));},120000);
   const cleanup=()=>{clearTimeout(timer);options.signal?.removeEventListener('abort',abort);};
   pending.set(id,{resolve,reject,cleanup});options.signal?.addEventListener('abort',abort,{once:true});
   value({kind:'request',id,operation,pdf,confirmed:options.headers?.['X-Confirm-Courses']==='yes',settings:operation==='audit'?JSON.parse(new URL(url,location.href).searchParams.get('settings')):{}});
  });
 };
 document.querySelector('#clear-transcript').addEventListener('click',()=>value({kind:'clear'}));
 send('streamlit:componentReady',{apiVersion:1});
 send('streamlit:setFrameHeight',{height:900});
})();
