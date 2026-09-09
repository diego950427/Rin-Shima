// Loader artwork: zanina-yassine / Uiverse (MIT), supplied mountain package.
(() => {
 const loader=document.querySelector('#page-loader');
 if(!loader)return;
 // Cloud wraps the entire page in a scroller; only inert loader siblings.
 const siblings=[...loader.parentElement.children].filter(n=>n!==loader && !['SCRIPT','STYLE','NOSCRIPT'].includes(n.tagName));
 siblings.forEach(n=>n.inert=true);
 let finished=false;
 const finish=()=>{
  if(finished)return;finished=true;
  clearTimeout(deadline);siblings.forEach(n=>n.inert=false);
  loader.classList.add('is-ready');
  const remove=()=>loader.remove();
  loader.addEventListener('transitionend',remove,{once:true});
  setTimeout(remove,400);
 };
 const deadline=setTimeout(finish,12000);
 const ready=document.readyState==='complete'?Promise.resolve():new Promise(resolve=>addEventListener('load',resolve,{once:true}));
 Promise.all([ready,document.fonts?.ready||Promise.resolve()]).then(finish,finish);
})();
