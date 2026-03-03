import logging
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

logger = logging.getLogger("oc_db_kyoo")

router = APIRouter()

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>oc_db_kyoo Dashboard</title>
<style>
  :root {
    --bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;
    --text:#e1e4ed;--muted:#8b8fa3;
    --green:#34d399;--yellow:#fbbf24;--red:#f87171;
    --blue:#60a5fa;--purple:#a78bfa;--orange:#fb923c;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
    background:var(--bg);color:var(--text);min-height:100vh;padding:24px;
  }
  header{display:flex;justify-content:space-between;align-items:center;margin-bottom:32px;flex-wrap:wrap;gap:12px}
  header h1{font-size:1.5rem;font-weight:600;letter-spacing:-0.02em}
  header h1 span{color:var(--muted);font-weight:400}
  .pill{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:20px;font-size:0.8rem;font-weight:500;text-transform:uppercase;letter-spacing:0.04em}
  .pill-ok{background:rgba(52,211,153,0.12);color:var(--green)}
  .pill-bad{background:rgba(248,113,113,0.12);color:var(--red)}
  .dot{width:8px;height:8px;border-radius:50%;background:currentColor;animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
  .meta{display:flex;gap:16px;align-items:center;color:var(--muted);font-size:0.8rem}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:20px}

  /* Card with circuit-state top border */
  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:24px;transition:border-color 0.2s}
  .card:hover{border-color:#3a3d4a}
  .card-closed{border-top:3px solid var(--green)}
  .card-open{border-top:3px solid var(--red)}
  .card-half_open{border-top:3px solid var(--orange)}

  .card-hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
  .card-hd h2{font-size:1.1rem;font-weight:600}

  /* Circuit state indicator */
  .circuit{display:flex;align-items:center;gap:10px;padding:10px 14px;border-radius:8px;margin-bottom:18px;font-size:0.82rem;font-weight:500}
  .circuit-closed{background:rgba(52,211,153,0.08);color:var(--green)}
  .circuit-open{background:rgba(248,113,113,0.1);color:var(--red)}
  .circuit-half_open{background:rgba(251,146,60,0.1);color:var(--orange)}
  .circuit-icon{width:20px;height:20px;flex-shrink:0}
  .circuit-label{flex:1}
  .circuit-breaks{font-size:0.72rem;color:var(--muted);font-weight:400}

  .badge{font-size:0.75rem;padding:3px 10px;border-radius:12px;font-weight:500}
  .b-lo{background:rgba(52,211,153,0.12);color:var(--green)}
  .b-mi{background:rgba(251,191,36,0.12);color:var(--yellow)}
  .b-hi{background:rgba(248,113,113,0.12);color:var(--red)}
  .meter{margin-bottom:14px}
  .meter-lbl{display:flex;justify-content:space-between;font-size:0.78rem;color:var(--muted);margin-bottom:6px}
  .meter-track{height:6px;background:var(--border);border-radius:3px;overflow:hidden}
  .meter-fill{height:100%;border-radius:3px;transition:width 0.6s ease,background 0.3s}
  .sg{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-top:16px;padding-top:16px;border-top:1px solid var(--border)}
  .s{display:flex;flex-direction:column;gap:2px}
  .sv{font-size:1.2rem;font-weight:600;font-variant-numeric:tabular-nums}
  .sl{font-size:0.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em}
  .c-g{color:var(--green)}.c-r{color:var(--red)}.c-y{color:var(--yellow)}
  .c-b{color:var(--blue)}.c-p{color:var(--purple)}.c-o{color:var(--orange)}
  #err{display:none;background:rgba(248,113,113,0.1);border:1px solid rgba(248,113,113,0.3);color:var(--red);padding:12px 20px;border-radius:8px;margin-bottom:20px;font-size:0.85rem}
  .empty{text-align:center;padding:80px 20px;color:var(--muted)}

  /* Circuit summary bar */
  .summary{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}
  .summary-item{display:flex;align-items:center;gap:8px;padding:8px 16px;border-radius:8px;background:var(--surface);border:1px solid var(--border);font-size:0.82rem}
  .summary-count{font-weight:700;font-size:1.1rem;font-variant-numeric:tabular-nums}
</style>
</head>
<body>
<header>
  <div><h1>oc_db_kyoo <span>dashboard</span></h1></div>
  <div class="meta">
    <span id="ts">&mdash;</span>
    <span id="gs"></span>
  </div>
</header>
<div id="err"></div>
<div id="summary" class="summary"></div>
<div id="g" class="grid"><div class="empty"><p>Loading backends&hellip;</p></div></div>
<script>
const REFRESH=2000;
const g=document.getElementById('g'),gs=document.getElementById('gs'),
      ts=document.getElementById('ts'),eb=document.getElementById('err'),
      sm=document.getElementById('summary');

function fmt(n){return n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'K':String(n)}
function bc(r){return r<.5?'b-lo':r<.8?'b-mi':'b-hi'}
function mc(r){return r<.5?'var(--green)':r<.8?'var(--yellow)':'var(--red)'}

/* SVG icons for circuit states */
var icons={
  closed:'<svg class="circuit-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="10" cy="10" r="7"/><path d="M7 10l2 2 4-4"/></svg>',
  open:'<svg class="circuit-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="10" cy="10" r="7"/><path d="M7.5 7.5l5 5M12.5 7.5l-5 5"/></svg>',
  half_open:'<svg class="circuit-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="10" cy="10" r="7"/><path d="M10 6.5v4l2.5 1.5"/></svg>'
};

var circuitLabels={
  closed:'Circuit CLOSED — operational',
  open:'Circuit OPEN — backend down',
  half_open:'Circuit HALF_OPEN — testing recovery'
};

function circuitHtml(b){
  var cs=b.circuit_state||'closed';
  var tb=b.total_circuit_breaks||0;
  var breaksTxt=tb>0?' (tripped '+tb+' time'+(tb>1?'s':'')+')'  :'';
  return '<div class="circuit circuit-'+cs+'">'+
    (icons[cs]||icons.closed)+
    '<span class="circuit-label">'+(circuitLabels[cs]||cs)+'</span>'+
    '<span class="circuit-breaks">'+breaksTxt+'</span>'+
    '</div>';
}

function card(b){
  var a=b.active_requests||0, q=b.queued_requests||0,
      tot=b.total_requests||0, comp=b.total_completed||0,
      err=b.total_errors||0, to=b.total_timeouts||0,
      rej=b.total_rejected||0, avg=b.avg_response_time_ms||0;
  var cs=b.circuit_state||'closed';
  var est=Math.max(a+q,10), ar=a/est, qr=q/Math.max(est*5,50);
  return '<div class="card card-'+cs+'">'+
    '<div class="card-hd"><h2>'+b.name+'</h2>'+
    '<span class="badge '+bc(ar)+'">'+a+' active / '+q+' queued</span></div>'+
    circuitHtml(b)+
    '<div class="meter"><div class="meter-lbl"><span>Active</span><span>'+a+'</span></div>'+
    '<div class="meter-track"><div class="meter-fill" style="width:'+Math.min(ar*100,100)+'%;background:'+mc(ar)+'"></div></div></div>'+
    '<div class="meter"><div class="meter-lbl"><span>Queued</span><span>'+q+'</span></div>'+
    '<div class="meter-track"><div class="meter-fill" style="width:'+Math.min(qr*100,100)+'%;background:'+mc(qr)+'"></div></div></div>'+
    '<div class="sg">'+
    '<div class="s"><span class="sv c-b">'+fmt(tot)+'</span><span class="sl">Total</span></div>'+
    '<div class="s"><span class="sv c-g">'+fmt(comp)+'</span><span class="sl">Completed</span></div>'+
    '<div class="s"><span class="sv c-r">'+fmt(err)+'</span><span class="sl">Errors</span></div>'+
    '<div class="s"><span class="sv c-y">'+fmt(to)+'</span><span class="sl">Timeouts</span></div>'+
    '<div class="s"><span class="sv c-p">'+avg.toFixed(0)+'<span style="font-size:0.7rem;color:var(--muted)"> ms</span></span><span class="sl">Avg response</span></div>'+
    '<div class="s"><span class="sv c-r">'+fmt(rej)+'</span><span class="sl">Rejected</span></div>'+
    '</div></div>';
}

function summaryBar(backends){
  var closed=0,open=0,half=0;
  backends.forEach(function(b){
    var cs=b.circuit_state||'closed';
    if(cs==='closed') closed++;
    else if(cs==='open') open++;
    else half++;
  });
  var h='';
  h+='<div class="summary-item" style="color:var(--green)">'+icons.closed+'<span class="summary-count">'+closed+'</span> healthy</div>';
  if(open>0) h+='<div class="summary-item" style="color:var(--red)">'+icons.open+'<span class="summary-count">'+open+'</span> down</div>';
  if(half>0) h+='<div class="summary-item" style="color:var(--orange)">'+icons.half_open+'<span class="summary-count">'+half+'</span> recovering</div>';
  return h;
}

async function poll(){
  try{
    var r=await fetch('status');
    if(!r.ok) throw new Error('HTTP '+r.status);
    var d=await r.json();
    eb.style.display='none';
    var st=d.status||'unknown';
    gs.innerHTML='<span class="pill '+(st==='ok'?'pill-ok':'pill-bad')+'"><span class="dot"></span>'+st+'</span>';
    var bk=d.backends||[];
    sm.innerHTML=bk.length?summaryBar(bk):'';
    g.innerHTML=bk.length?bk.map(card).join(''):'<div class="empty"><p>No backends.</p></div>';
    ts.textContent='Updated '+new Date().toLocaleTimeString();
  }catch(e){
    eb.textContent='Failed to fetch /status \u2014 '+e.message;
    eb.style.display='block';
  }
}
poll();
setInterval(poll,REFRESH);
</script>
</body>
</html>"""


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Real-time monitoring dashboard. Auto-refreshes every 2 seconds."""
    return DASHBOARD_HTML