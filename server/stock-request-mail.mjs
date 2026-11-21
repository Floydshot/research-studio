/** Server-only mail transport; recipient and sender are configured as secrets. */
async function readKey(env) {
  const regional=env.SECRET_RESEND_API_KEY;
  const value=regional!==undefined?regional&&typeof regional.get==='function'?await regional.get():null:env.RESEND_API_KEY;
  if(typeof value!=='string'||value.trim().length<20||/[\r\n]/.test(value))throw new Error('configuration_unavailable');
  return value.trim();
}

export function withStockRequestMailer(env) {
  if(env.STOCK_REQUEST_EMAIL&&typeof env.STOCK_REQUEST_EMAIL.send==='function')return env;
  return {...env,STOCK_REQUEST_EMAIL:{async send(message){
    const key=await readKey(env),controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),7000);
    try {
      const result=await fetch('https://api.resend.com/emails',{
        method:'POST',signal:controller.signal,
        headers:{Authorization:'Bearer '+key,'Content-Type':'application/json','Idempotency-Key':message.idempotencyKey},
        body:JSON.stringify({from:message.from,to:[message.to],subject:message.subject,text:message.text})
      });
      controller.signal.throwIfAborted();
      if(!result.ok)throw new Error('provider_rejected');
      let accepted;try{accepted=await result.json();}catch(_){throw new Error('provider_rejected');}
      controller.signal.throwIfAborted();
      if(typeof accepted?.id!=='string'||!accepted.id.trim())throw new Error('provider_rejected');
      return {accepted:true};
    }finally{clearTimeout(timer);}
  }}};
}
