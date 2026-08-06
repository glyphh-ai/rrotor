import { chromium } from 'playwright-core';
const S='http://127.0.0.1:3001', W='http://localhost:3002';
const st=await fetch(`${S}/api/auth/magic/start`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({email:'timmetim@gmail.com'})}).then(r=>r.text());
const tok=st.match(/token=([A-Za-z0-9_.-]+)/)[1];
const b=await chromium.launch({headless:true,executablePath:'/Users/timmetim/Library/Caches/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-mac-arm64/chrome-headless-shell'});
const p=await(await b.newContext({viewport:{width:1280,height:860}})).newPage();
const errs=[]; p.on('console',m=>{if(m.type()==='error')errs.push(m.text().slice(0,220));}); p.on('pageerror',e=>errs.push('PAGEERR '+String(e).slice(0,220)));
const bad=[]; p.on('response',r=>{if(r.status()>=400 && /api/.test(r.url()))bad.push(r.status()+' '+r.url().replace(S,'').slice(0,50));});
await p.goto(`${W}/auth/callback?token=${tok}`,{waitUntil:'domcontentloaded'});await p.waitForTimeout(5000);
await p.goto(`${W}/work`,{waitUntil:'domcontentloaded'});await p.waitForTimeout(5000);
const chip=await p.locator('.cl-runtime-chip').count(); console.log('chip present:',chip);
await p.locator('.cl-runtime-chip').first().click({force:true}); await p.waitForTimeout(1500);
const popup=await p.locator('.cl-runtime-pop, .cl-src-pop, [class*="runtime-pop"], [class*="src-pop"]').count();
console.log('popup nodes:',popup);
const html=await p.evaluate(()=>{const el=document.querySelector('.cl-runtime-chip-wrap');return el?el.innerHTML.slice(0,700):'(no wrap)';});
console.log('WRAP HTML:',html);
const dropM = await p.evaluate(() => {
  const d = document.querySelector('.ws-source-drop');
  if (!d) return '(no drop)';
  const r = d.getBoundingClientRect(); const cs = getComputedStyle(d);
  // find first ancestor clipping it
  let clip='none', e=d.parentElement;
  while(e){ const c=getComputedStyle(e); if(c.overflow!=='visible'||c.overflowY!=='visible'||c.overflowX!=='visible'){clip=e.className.toString().slice(0,40)+' ('+c.overflow+'/'+c.overflowX+'/'+c.overflowY+')'; break;} e=e.parentElement; }
  return { rect:`${Math.round(r.width)}x${Math.round(r.height)}@${Math.round(r.x)},${Math.round(r.y)}`, display:cs.display, vis:cs.visibility, opacity:cs.opacity, z:cs.zIndex, pos:cs.position, clippedBy:clip };
});
console.log('DROP:', JSON.stringify(dropM));
console.log('bad api:',JSON.stringify(bad.slice(0,6)));
console.log('errors:',JSON.stringify(errs.slice(0,6)));
await b.close();
