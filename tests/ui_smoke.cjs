// Real browser against the real local backend, through a Docker-host bridge.
const {chromium}=require('/opt/evo-browser/node_modules/playwright');
const fs=require('fs');
async function bridge(page){
 await page.route('http://crew.local/**',async route=>{
  const req=route.request(),u=new URL(req.url());
  const headers={...req.headers(),host:'127.0.0.1:8100',origin:'http://127.0.0.1:8100'};
  delete headers['content-length'];
  try{
   const r=await page.request.fetch('http://host.docker.internal:8100'+u.pathname+u.search,{method:req.method(),headers,data:req.postDataBuffer()||undefined,timeout:30000});
   const h={...r.headers()};delete h['content-encoding'];delete h['transfer-encoding'];delete h['content-length'];
   await route.fulfill({status:r.status(),headers:h,body:await r.body()});
  }catch(e){try{await route.abort();}catch{}}
 });
}
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1480,height:1050}});await bridge(page);
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://crew.local/');await page.locator('#channels [data-channel="dev"]').waitFor();
 if(!await page.locator('#model-pill').innerText().then(t=>t.includes('deepseek')))throw Error('Real model configuration did not load');
 await page.locator('[data-view="agents"]').click();await page.locator('[data-edit-agent="dev"]').click();await page.locator('#agent-role').waitFor();await page.locator('#close-dialog').click();
 await page.locator('[data-view="evaluation"]').click();await page.locator('#start-suite').click();await page.locator('#suite-budget').waitFor();await page.locator('#close-dialog').click();
 await page.screenshot({path:'/evidence/evaluation.png',fullPage:true});
 await page.locator('[data-view="workspace"]').click();await page.locator('[data-channel="dev"]').click();
 await page.locator('#prompt').fill('界面澄清验收：请先使用 ask_user 问我“清单标题是什么？”。收到实际答复后只用一句话确认标题，不写文件、不执行代码、不操作 Git。');await page.locator('#send').click();
 const card=page.locator('[data-question]').first();await card.waitFor({timeout:90000});
 await page.screenshot({path:'/evidence/human-clarification.png',fullPage:true});
 await card.locator('textarea').fill('界面验收清单');await card.locator('[data-answer="text"]').click();
 await page.waitForFunction(()=>document.querySelector('#run-summary .badge')?.textContent==='已完成',{},{timeout:90000});
 await page.locator('[data-channel="group"]').click();await page.waitForTimeout(1800);
 if((await page.locator('#messages').innerText()).includes('界面验收清单'))throw Error('Private message leaked into group conversation');
 await page.locator('[data-tab="tasks"]').click();await page.screenshot({path:'/evidence/workspace.png',fullPage:true});
 const mobile=await browser.newPage({viewport:{width:390,height:844}});await bridge(mobile);await mobile.goto('http://crew.local/');await mobile.locator('#channels [data-channel="dev"]').waitFor();
 const overflow=await mobile.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+1);if(overflow)throw Error('Mobile layout overflows horizontally');await mobile.screenshot({path:'/evidence/mobile.png',fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
 fs.writeFileSync('/evidence/browser-ui-check.json',JSON.stringify({passed:true,checks:['real backend configuration','role editor','evaluation and suite dialog','actual model clarification','actual human answer','private/group isolation','desktop screenshot','390px mobile no horizontal overflow','no page errors']},null,2));
 await browser.close();console.log('Real UI browser checks passed');
})().catch(e=>{console.error(e);process.exit(1)});
