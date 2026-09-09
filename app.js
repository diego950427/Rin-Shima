const menu = document.querySelector('.menu-toggle');
const nav = document.querySelector('#navigation');
function closeMenu(){nav.hidden=true;menu.setAttribute('aria-expanded','false');menu.setAttribute('aria-label','開啟選單');}
menu.addEventListener('click',()=>{const open=menu.getAttribute('aria-expanded')!=='true';nav.hidden=!open;menu.setAttribute('aria-expanded',String(open));menu.setAttribute('aria-label',open?'關閉選單':'開啟選單');});
nav.addEventListener('click',e=>{if(e.target.closest('a'))closeMenu();});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!nav.hidden){closeMenu();menu.focus();}});
document.addEventListener('click',e=>{if(!nav.hidden&&!e.target.closest('.site-header'))closeMenu();});
const art=document.querySelector('#art'), still=document.querySelector('#still'), motion=document.querySelector('#motion');
function pauseArt(paused){
  if(paused){if(!art.complete||!art.naturalWidth)return;still.getContext('2d').drawImage(art,0,0,600,864);}
  still.hidden=!paused;art.hidden=paused;motion.setAttribute('aria-pressed',String(paused));motion.textContent=paused?'播放動畫':'暫停動畫';
}
motion.addEventListener('click',()=>pauseArt(motion.getAttribute('aria-pressed')!=='true'));
const reduce=matchMedia('(prefers-reduced-motion: reduce)');
if(reduce.matches){if(art.complete)pauseArt(true);else art.addEventListener('load',()=>pauseArt(true),{once:true});}
reduce.addEventListener('change',e=>pauseArt(e.matches));
