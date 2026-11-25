import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
const root=fileURLToPath(new URL('../',import.meta.url));
const assets={};
for(const [path,type]of [['/index.html','text/html; charset=utf-8'],['/client/project-workspaces.js','text/javascript; charset=utf-8'],['/client/overview-model.js','text/javascript; charset=utf-8'],['/client/custom-depots.js','text/javascript; charset=utf-8'],['/client/project-import.js','text/javascript; charset=utf-8'],['/client/project-removal.js','text/javascript; charset=utf-8'],['/client/stock-requests.js','text/javascript; charset=utf-8'],['/vendor/chart.umd.min.js','text/javascript; charset=utf-8']])assets[path]={type,body:await readFile(root+path,'utf8')};
const api="import {handleProjectApi} from '../server/project-api.mjs';\nimport {withStockRequestMailer} from '../server/stock-request-mail.mjs';\nimport {runRefreshScheduler} from '../server/refresh-scheduler.mjs';\nexport {ProjectPasswordVerifier} from '../server/password-verifier.mjs';\n";
const code=api+'\nconst PUBLIC_ASSETS='+JSON.stringify(assets)+';\n'+String.raw`
async function stockMailConfigured(env){const get=async name=>{try{const regional=env['SECRET_'+name],value=regional!==undefined?regional&&typeof regional.get==='function'?await regional.get():null:env[name];return typeof value==='string'&&value.trim()?value:null;}catch(_){return null;}};const[from,to]=await Promise.all([get('STOCK_REQUEST_FROM'),get('STOCK_REQUEST_TO')]);if(!from||from.length>320||/[\r\n]/.test(from)||!to||!/^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$/.test(to))return false;if(typeof env.STOCK_REQUEST_EMAIL?.send==='function')return true;const key=await get('RESEND_API_KEY');return !!key&&key.trim().length>=20&&!/[\r\n]/.test(key);}
function publicHeaders(type,cache='no-cache') {return {'Content-Type':type,'Cache-Control':cache,'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"};}
export default {
 async scheduled(controller,env) {
  const outcome=await runRefreshScheduler(controller,env);
  console.log('market-refresh-scheduler',JSON.stringify(outcome));
  if(outcome.outcome==='attempts_exhausted')throw new Error('Daily market refresh was not published after three attempts');
 },
 async fetch(request,env) {
  const url=new URL(request.url);
  try {
   if(url.pathname.startsWith('/api/'))return await handleProjectApi(request,withStockRequestMailer(env))||new Response('Nicht gefunden',{status:404});
   if(request.method!=='GET'&&request.method!=='HEAD')return new Response('Methode nicht erlaubt',{status:405,headers:{Allow:'GET, HEAD'}});
   if(url.pathname==='/data.json'||url.pathname==='/refresh-status.json') {
    const source='https://floydshot.github.io/research-studio'+url.pathname;
    const upstream=await fetch(source,{headers:{Accept:'application/json'},cf:{cacheTtl:url.searchParams.has('_refresh')?0:60}});
    if(!upstream.ok)return new Response('Marktdaten sind gerade nicht erreichbar.',{status:503,headers:publicHeaders('text/plain; charset=utf-8','no-store')});
    return new Response(request.method==='HEAD'?null:upstream.body,{headers:publicHeaders('application/json; charset=utf-8',url.searchParams.has('_refresh')?'no-store':'public, max-age=60')});
   }
   const key=url.pathname==='/'?'/index.html':url.pathname,asset=PUBLIC_ASSETS[key];
   if(!asset)return new Response('Nicht gefunden',{status:404,headers:publicHeaders('text/plain; charset=utf-8')});
   const body=key==='/index.html'&&!await stockMailConfigured(env)?asset.body.replace('<meta name="stock-requests-enabled" content="true">','<meta name="stock-requests-enabled" content="false">'):asset.body;
   return new Response(request.method==='HEAD'?null:body,{headers:publicHeaders(asset.type)});
  } catch (_) { return new Response('Gerade nicht verfügbar.',{status:503,headers:{'Content-Type':'text/plain; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}}); }
 }
};
`;
await mkdir(root+'/dist',{recursive:true});await writeFile(root+'/dist/worker.mjs',code);console.log('Worker built:',Buffer.byteLength(code),'bytes; public shell + isolated project API');
