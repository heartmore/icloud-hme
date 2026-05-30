#!/usr/bin/env python3
"""
iCloud HME Web UI — 暖色主题管理界面
======================================
Flask 单页应用。

用法:
    python web_ui.py                              # 0.0.0.0:5050
    python web_ui.py --port 8080 --scheduler      # 自动启动调度器
    python web_ui.py --cookies cookies.json       # 手动 cookie
"""

import sys, os, json, time, queue, secrets, threading
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from flask import Flask, Response, request, jsonify, render_template_string
from icloud_hme import ICloudHME, extract_chrome_cookies

# ---- config ----
RESULTS_DIR = HERE / "results"
LOGS_DIR = HERE / "logs"
COOKIE_FILE = HERE / "cookies.json"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ---- global state ----
app = Flask(__name__)
_log_queue = queue.Queue()

_today_key = datetime.now().strftime("%Y%m%d")

_global_state = {
    "running": False,
    "creating": False,
    "rate_limited": False,
    "round_status": "",
    "total_created": 0,
    "today_created": 0,
    "current_round_created": 0,
    "next_trigger": None,
    "last_error": None,
    "cookies_ok": False,
    "alias_count": 0,
    "alias_active": 0,
}
_lock = threading.Lock()
_scheduler_thread = None
_stop_event = threading.Event()
_icloud_client = None


# iCloud 限流相关的错误关键词 (英文+中文)
_RATE_LIMIT_KW = ["limit","exceeded","maximum","quota","429","too many",
                   "try again","unavailable","上限","超过","过多","频繁",
                   "rate limit","throttle","blocked","分钟","hour","minute"]

def _is_limit_error(err: str) -> bool:
    return any(kw in err.lower() for kw in _RATE_LIMIT_KW)


# ---- 网络时间校准 ----
_time_offset = 0.0  # 网络时间 - 本地时间 (秒)

def _sync_time():
    """联网校准时间，返回偏移秒数"""
    global _time_offset
    for url in ["https://www.baidu.com", "https://www.google.com",
                "https://www.cloudflare.com", "https://www.microsoft.com"]:
        try:
            import requests as _r
            resp = _r.head(url, timeout=5)
            date_str = resp.headers.get("Date", "")
            if date_str:
                from email.utils import parsedate_to_datetime
                net_time = parsedate_to_datetime(date_str)
                local_time = datetime.now()
                _time_offset = (net_time - local_time).total_seconds()
                return _time_offset
        except Exception:
            continue
    return 0.0


def _now() -> datetime:
    """返回校准后的当前时间"""
    return datetime.now() + timedelta(seconds=_time_offset)


def _emit_log(level, msg):
    _log_queue.put({"time": _now().strftime("%H:%M:%S"), "level": level, "msg": msg})


def _update_state(**kw):
    global _today_key
    with _lock:
        today = _now().strftime("%Y%m%d")
        if today != _today_key:
            _global_state["today_created"] = 0
            _today_key = today
        _global_state.update(kw)


def _save_cookies(data: dict):
    try:
        COOKIE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_saved_cookies():
    if COOKIE_FILE.exists():
        try:
            return json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


# ============================================================
UI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>iCloud HME</title>
<style>
:root {
  --bg:        #f8f6f3;
  --surface:   #ffffff;
  --surface2:  #f3f0eb;
  --border:    #e5e0d8;
  --text:      #2c2416;
  --text2:     #9b8c78;
  --accent:    #e07b28;
  --accent2:   #f09a50;
  --accent-glow: rgba(224,123,40,0.15);
  --green:     #5b9a3f;
  --green-bg:  rgba(91,154,63,0.10);
  --red:       #d94436;
  --red-bg:    rgba(217,68,54,0.10);
  --orange:    #e07b28;
  --orange-bg: rgba(224,123,40,0.10);
  --shadow:    0 2px 12px rgba(0,0,0,0.06);
  --radius:    12px;
  --radius-sm: 8px;
  --font:      -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  --mono:      "SF Mono","Fira Code","Cascadia Code",Consolas,monospace;
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh;display:flex}
/* sidebar */
.sidebar{
  width:220px;background:var(--surface);border-right:1px solid var(--border);
  padding:24px 16px;display:flex;flex-direction:column;gap:6px;flex-shrink:0
}
.sidebar .logo{font-size:20px;font-weight:700;letter-spacing:-0.3px;margin-bottom:20px;display:flex;align-items:center;gap:10px}
.sidebar .logo .icon{
  width:36px;height:36px;border-radius:10px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  display:flex;align-items:center;justify-content:center;font-size:18px
}
.sidebar .nav-item{
  padding:10px 14px;border-radius:var(--radius-sm);color:var(--text2);
  font-size:14px;cursor:pointer;transition:all .15s;user-select:none
}
.sidebar .nav-item:hover{background:var(--surface2);color:var(--text)}
.sidebar .nav-item.active{background:var(--accent);color:#fff;font-weight:600}
.status-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:8px;vertical-align:middle}
.status-dot.online{background:var(--green);box-shadow:0 0 6px var(--green)}
.status-dot.offline{background:var(--red)}
/* main */
.main{flex:1;padding:28px 32px;overflow-y:auto;display:flex;flex-direction:column;gap:22px}
.header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}
.header h1{font-size:24px;font-weight:700;letter-spacing:-0.4px}
/* cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.card{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:18px 20px;transition:all .2s;box-shadow:var(--shadow)
}
.card:hover{border-color:var(--accent);box-shadow:0 4px 20px rgba(0,0,0,0.10)}
.card .label{font-size:11px;color:var(--text2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.card .value{font-size:32px;font-weight:700;letter-spacing:-1px}
.card .value.accent{color:var(--accent)}
.card .value.green{color:var(--green)}
.card .value.orange{color:var(--orange)}
.card .sub{font-size:12px;color:var(--text2);margin-top:4px}
/* panel */
.panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow)}
.panel-header{
  padding:14px 20px;border-bottom:1px solid var(--border);
  display:flex;justify-content:space-between;align-items:center;font-weight:600;font-size:14px
}
.panel-body{padding:0}
/* buttons */
.btn{padding:8px 18px;border-radius:var(--radius-sm);font-size:13px;font-weight:600;cursor:pointer;border:none;font-family:var(--font);transition:all .15s}
.btn-primary{background:linear-gradient(135deg,var(--accent),#d06820);color:#fff}
.btn-primary:hover{box-shadow:0 0 18px var(--accent-glow)}
.btn-primary:disabled{opacity:.4;cursor:not-allowed;box-shadow:none}
.btn-outline{background:transparent;border:1px solid var(--border);color:var(--text)}
.btn-outline:hover{border-color:var(--accent);color:var(--accent)}
.btn-danger{background:var(--red-bg);color:var(--red);border:1px solid transparent}
.btn-danger:hover{border-color:var(--red)}
.btn-sm{padding:5px 12px;font-size:12px}
.btn-group{display:flex;gap:8px}
/* email table */
.email-table{width:100%;border-collapse:collapse}
.email-table th{text-align:left;padding:10px 20px;font-size:11px;color:var(--text2);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
.email-table td{padding:12px 20px;font-size:14px;border-bottom:1px solid var(--border);font-family:var(--mono)}
.email-table tr:hover td{background:var(--surface2)}
#recentList .email-row:hover{background:var(--surface2)}
#recentList .email-row .copy-btn{opacity:0;transition:opacity .15s}
#recentList .email-row:hover .copy-btn{opacity:1}
.email-table .copy-btn{background:none;border:none;color:var(--text2);cursor:pointer;font-size:15px;padding:2px 6px;border-radius:4px}
.email-table .copy-btn:hover{color:var(--accent);background:var(--accent-glow)}
/* toast */
.copy-toast{position:fixed;top:20px;right:20px;background:var(--green);color:#fff;padding:10px 20px;border-radius:var(--radius-sm);font-weight:600;font-size:14px;opacity:0;transform:translateY(-10px);transition:all .25s;pointer-events:none;z-index:999}
.copy-toast.show{opacity:1;transform:translateY(0)}
/* log */
.log-feed{max-height:300px;overflow-y:auto;padding:12px 20px;font-family:var(--mono);font-size:13px;line-height:1.7}
.log-feed .log-line{white-space:pre-wrap;word-break:break-all}
.log-line.info{color:var(--text2)}
.log-line.success{color:var(--green)}
.log-line.warn{color:var(--orange)}
.log-line.error{color:var(--red)}
.log-time{color:#665544;margin-right:6px}
/* empty */
.empty{text-align:center;padding:40px 20px;color:var(--text2)}
.empty .icon{font-size:40px;margin-bottom:10px}
/* modal */
.modal-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);z-index:999;display:flex;align-items:center;justify-content:center}
.modal-box{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:24px;width:90%;max-width:520px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.modal-box h3{margin-bottom:8px;font-size:18px}
.modal-box p{font-size:13px;color:var(--text2);margin-bottom:16px;line-height:1.5}
.modal-box textarea{width:100%;height:140px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:12px;font-family:var(--mono);font-size:12px;resize:vertical}
.modal-box textarea:focus{outline:none;border-color:var(--accent)}
.modal-actions{display:flex;gap:8px;margin-top:14px;justify-content:flex-end}
.modal-msg{margin-top:10px;font-size:13px}
/* responsive */
@media(max-width:768px){
  body{flex-direction:column}
  .sidebar{width:100%;flex-direction:row;flex-wrap:wrap;padding:12px 16px;gap:6px}
  .sidebar .logo{margin-bottom:0;margin-right:auto}
  .main{padding:16px}
  .cards{grid-template-columns:repeat(2,1fr)}
}
</style>
</head>
<body>

<aside class="sidebar">
  <div class="logo"><div class="icon">&#9993;</div>iCloud HME</div>
  <a class="nav-item active" data-tab="dashboard">&#9776; 仪表盘</a>
  <a class="nav-item" data-tab="emails">&#9993; 邮箱列表</a>
  <a class="nav-item" data-tab="logs">&#9776; 运行日志</a>
  <div style="margin-top:auto;padding-top:16px;border-top:1px solid var(--border);font-size:12px;color:var(--text2)">
    <div style="margin-bottom:6px"><span class="status-dot" id="cookieDot" style="background:var(--red)"></span><span id="cookieLabel">Cookie: 未连接</span></div>
    <div style="margin-bottom:8px"><span class="status-dot" id="schedDot"></span><span id="schedLabel">调度器: 就绪</span></div>
    <button class="btn btn-outline btn-sm" onclick="showCookieModal()" style="width:100%;font-size:11px">&#128273; 导入 Cookie</button>
  </div>
</aside>

<main class="main">
  <div class="header">
    <h1 id="tabTitle">&#9776; 仪表盘</h1>
    <div class="btn-group">
      <button class="btn btn-outline btn-sm" onclick="createOne()" id="btnCreateOne" disabled>&#9889; 创建一个</button>
      <button class="btn btn-outline btn-sm" onclick="createBatch()" id="btnBatch" disabled>&#128230; 批量创建</button>
      <button class="btn btn-primary btn-sm" onclick="toggleScheduler()" id="btnSched">&#9654; 启动调度器</button>
    </div>
  </div>

  <div id="view-dashboard">
    <div class="cards">
      <div class="card"><div class="label">累计创建</div><div class="value accent" id="statTotal">--</div><div class="sub">历史总计</div></div>
      <div class="card"><div class="label">今日创建</div><div class="value green" id="statToday">--</div><div class="sub" id="statTodayLabel"></div></div>
      <div class="card"><div class="label">当前轮次</div><div class="value orange" id="statRound">--</div><div class="sub" id="statRoundLabel">--</div></div>
      <div class="card"><div class="label">下次触发</div><div class="value" id="statNext" style="font-size:19px">--</div><div class="sub" id="statNextLabel"></div></div>
    </div>
    <div class="panel" style="margin-top:14px">
      <div class="panel-header">&#128200; 最近创建</div>
      <div class="panel-body"><div id="recentList" class="empty"><div class="icon">&#128236;</div>暂无创建记录</div></div>
    </div>
  </div>

  <div id="view-emails" style="display:none">
    <div class="panel">
      <div class="panel-header">
        <span>&#9993; 所有创建的邮箱</span>
        <div style="display:flex;gap:8px;align-items:center">
          <span style="font-size:12px;color:var(--text2)" id="emailCount">0</span>
          <button class="btn btn-outline btn-sm" onclick="copyAll()">&#128203; 复制全部</button>
          <button class="btn btn-outline btn-sm" onclick="exportCSV()">&#8615; 导出 CSV</button>
        </div>
      </div>
      <div class="panel-body"><div id="emailListContainer" class="empty"><div class="icon">&#128236;</div>还没有创建邮箱</div></div>
    </div>
  </div>

  <div id="view-logs" style="display:none">
    <div class="panel">
      <div class="panel-header"><span>&#9776; 实时日志</span><button class="btn btn-outline btn-sm" onclick="clearLogs()">清屏</button></div>
      <div class="panel-body"><div class="log-feed" id="logFeed"></div></div>
    </div>
  </div>
</main>

<div class="copy-toast" id="toast"></div>

<script>
var E = function(id){return document.getElementById(id)};
var emails = [], logs = [], sseConn = null;
var state = {running:false,creating:false,rate_limited:false,round_status:'',total_created:0,today_created:0,current_round_created:0,next_trigger:null,cookies_ok:false,alias_count:0};
var curTab = 'dashboard';
var pollTimer = null;

// ---- tab switching ----
document.querySelectorAll('.nav-item').forEach(function(el){
  el.addEventListener('click',function(){
    curTab = this.dataset.tab;
    document.querySelectorAll('.nav-item').forEach(function(n){n.classList.remove('active')});
    this.classList.add('active');
    E('view-dashboard').style.display = curTab==='dashboard'?'block':'none';
    E('view-emails').style.display = curTab==='emails'?'block':'none';
    E('view-logs').style.display = curTab==='logs'?'block':'none';
    var titles = {dashboard:'仪表盘',emails:'邮箱列表',logs:'运行日志'};
    E('tabTitle').textContent = titles[curTab]||curTab;
    if(curTab==='logs') renderLogs();
  });
});

// ---- api ----
async function api(path,opts){
  try{var r=await fetch(path,opts);return r.json()}catch(e){return{ok:false,error:e.message}}
}

// ---- refresh ----
async function refreshState(){
  var d = await api('/api/state');
  state = d; renderState();
}
async function refreshEmails(){
  var d = await api('/api/emails');
  emails = d.emails||[]; renderEmails();
}
function renderState(){
  E('statTotal').textContent = state.total_created||'0';
  E('statToday').textContent = state.today_created||'0';
  E('statRound').textContent = state.current_round_created||'0';
  E('statRoundLabel').textContent = state.round_status||'';
  if(state.next_trigger){
    var dt = new Date(state.next_trigger * 1000);
    E('statNext').textContent = dt.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
    var diff = Math.max(0,(dt - new Date())/60000);
    E('statNextLabel').textContent = diff>1?Math.round(diff)+' 分钟后':'即将触发';
  }else{E('statNext').textContent = '--';E('statNextLabel').textContent = '调度器未运行'}
  var dot = E('schedDot');
  dot.className = 'status-dot '+(state.running?'online':'offline');
  var sm;
  if(!state.running)sm='已停止';
  else if(state.rate_limited)sm='已达上限,等待整点';
  else if(state.creating)sm='创建中...';
  else sm='等待整点触发';
  E('schedLabel').textContent='调度器: '+sm;
  var bs = E('btnSched');
  bs.textContent = state.running?'⏸ 停止调度器':'▶ 启动调度器';
  bs.className = 'btn btn-sm '+(state.running?'btn-danger':'btn-primary');
  var cd = E('cookieDot');
  var cl = E('cookieLabel');
  if (state.cookies_ok) {
    cd.style.background = 'var(--green)';
    cl.textContent = 'Cookie: 有效 (' + (state.alias_active||0) + '/' + (state.alias_count||0) + ' 活跃)';
    cl.title = '';
  } else if (state.last_error) {
    cd.style.background = 'var(--red)';
    cl.textContent = 'Cookie: ' + state.last_error;
    cl.title = state.last_error;
  } else {
    cd.style.background = 'var(--red)';
    cl.textContent = 'Cookie: 未连接';
    cl.title = '';
  }
  var ok = state.cookies_ok && !state.creating;
  E('btnCreateOne').disabled = !ok;
  E('btnBatch').disabled = !ok;
}
function renderEmails(){
  E('emailCount').textContent = emails.length;
  var c = E('emailListContainer');
  if(!emails.length){
    c.innerHTML='<div class="empty"><div class="icon">&#128236;</div>还没有创建邮箱</div>';
  } else {
    var h='<table class="email-table"><thead><tr><th>#</th><th>邮箱地址</th><th>创建时间</th><th></th></tr></thead><tbody>';
    emails.forEach(function(e,i){
      var addr = esc(e.email);
      h+='<tr><td style="color:var(--text2);width:50px">'+(i+1)+'</td><td>'+addr+'</td><td style="font-size:12px;color:var(--text2)">'+esc(e.created_at||'--')+'</td><td style="width:40px"><button class="copy-btn" data-email="'+escAttr(e.email)+'" title="复制">&#128203;</button></td></tr>';
    });
    h+='</tbody></table>';
    c.innerHTML = h;
    c.querySelectorAll('.copy-btn').forEach(function(b){
      b.addEventListener('click',function(){copyOne(this.dataset.email)});
    });
  }
  // 仪表盘「最近创建」
  var rl = E('recentList');
  if(!emails.length){
    rl.innerHTML='<div class="empty"><div class="icon">&#128236;</div>暂无创建记录</div>';
  } else {
    var recent = emails.slice(-8).reverse();
    rl.innerHTML = recent.map(function(e){
      return '<div class="email-row" style="padding:8px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center"><span style="font-family:var(--mono);font-size:13px">'+esc(e.email)+'</span><button class="copy-btn" data-email="'+escAttr(e.email)+'" style="font-size:11px">复制</button></div>';
    }).join('');
    rl.querySelectorAll('.copy-btn').forEach(function(b){
      b.addEventListener('click',function(){copyOne(this.dataset.email)});
    });
  }
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function escAttr(s){return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}

// ---- actions ----
async function createOne(){
  var b=E('btnCreateOne');b.disabled=true;b.textContent='...';
  var d=await api('/api/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count:1})});
  b.disabled=false;b.textContent='创建 1 个';
  if(d.ok)toast('创建成功: '+d.emails[0]);else toast('失败: '+(d.error||'?'),true);
  refreshState();refreshEmails();
}
async function createBatch(){
  var n=prompt('批量创建数量:','5');if(!n)return;
  var b=E('btnBatch');b.disabled=true;b.textContent='...';
  var d=await api('/api/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count:parseInt(n)})});
  b.disabled=false;b.textContent='批量创建';
  if(d.ok)toast('成功创建 '+d.emails.length+' 个');else toast('失败: '+(d.error||'?'),true);
  refreshState();refreshEmails();
}
async function toggleScheduler(){
  var act=state.running?'stop':'start';
  var d=await api('/api/scheduler/'+act,{method:'POST'});
  if(d.ok)toast(state.running?'调度器已停止':'调度器已启动');
  refreshState();
}
function copyOne(email){
  navigator.clipboard.writeText(email).then(function(){toast('已复制: '+email)});
}
function copyAll(){
  navigator.clipboard.writeText(emails.map(function(e){return e.email}).join('\n')).then(function(){toast('已复制 '+emails.length+' 个')});
}
function exportCSV(){
  var csv='email,created_at\n'+emails.map(function(e){return e.email+','+(e.created_at||'')}).join('\n');
  var b=new Blob([csv],{type:'text/csv'}),a=document.createElement('a');
  a.href=URL.createObjectURL(b);a.download='icloud_emails.csv';a.click();
}
function clearLogs(){logs=[];E('logFeed').innerHTML=''}
function toast(msg,isErr){
  var t=E('toast');t.textContent=(isErr?'✗ ':'✓ ')+msg;
  t.style.background=isErr?'#d94436':'#5b9a3f';t.style.color='#fff';
  t.classList.add('show');
  setTimeout(function(){t.classList.remove('show')},2200);
}

// ---- SSE ----
function connectSSE(){
  if(sseConn){sseConn.close();sseConn=null}
  sseConn = new EventSource('/api/log-stream');
  sseConn.onmessage = function(e){
    try{
      var entry=JSON.parse(e.data);
      logs.push(entry);if(logs.length>500)logs=logs.slice(-500);
      if(curTab==='logs')renderLogs();
      if(entry.msg && entry.msg.indexOf('创建')>=0){refreshState();refreshEmails()}
    }catch(_){}
  };
  sseConn.onerror = function(){sseConn.close();sseConn=null;setTimeout(connectSSE,5000)};
}
function renderLogs(){
  var f=E('logFeed');
  f.innerHTML = logs.map(function(l){
    return '<div class="log-line '+l.level+'"><span class="log-time">'+esc(l.time)+'</span>'+esc(l.msg)+'</div>';
  }).join('\n');
  f.scrollTop = f.scrollHeight;
}

// ---- cookie modal ----
function showCookieModal(){
  var h='<div class="modal-overlay" id="cookieModal" onclick="if(event.target===this)closeCookieModal()">'+
    '<div class="modal-box"><h3>&#128273; 导入 iCloud Cookie</h3>'+
    '<p>使用 <b>Cookie Editor</b> 扩展导出 <b>Header String</b>，粘贴到下方</p>'+
    '<textarea id="cookieInput" placeholder="粘贴 cookie header string ..."></textarea>'+
    '<div class="modal-actions"><button class="btn btn-outline btn-sm" onclick="closeCookieModal()">取消</button>'+
    '<button class="btn btn-primary btn-sm" id="btnImportCookie" onclick="importCookies()">导入</button></div>'+
    '<div class="modal-msg" id="cookieMsg"></div></div></div>';
  document.body.insertAdjacentHTML('beforeend',h);
}
function closeCookieModal(){var m=E('cookieModal');if(m)m.remove()}
async function importCookies(){
  var raw=E('cookieInput').value.trim();if(!raw)return;
  var b=E('btnImportCookie');b.disabled=true;b.textContent='导入中...';
  var d=await api('/api/set-cookies',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookies:raw})});
  b.disabled=false;b.textContent='导入';
  var m=E('cookieMsg');
  if(d.ok){m.innerHTML='<span style="color:var(--green)">&#10003; 导入成功! '+d.alias_count+' 个别名</span>';setTimeout(closeCookieModal,1200);refreshState()}
  else{m.innerHTML='<span style="color:var(--red)">&#10007; '+esc(d.error||'失败')+'</span>'}
}

// ---- init ----
refreshState();refreshEmails();connectSSE();
pollTimer = setInterval(function(){refreshState();refreshEmails()},12000);
</script>
</body>
</html>"""


# ============================================================
# Flask routes
# ============================================================

@app.route("/")
def index():
    return render_template_string(UI_HTML)


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify(dict(_global_state))


@app.route("/api/emails")
def api_emails():
    emails = []
    f = RESULTS_DIR / "latest_emails.txt"
    if f.exists():
        for line in f.read_text(encoding="utf-8").strip().split("\n"):
            line = line.strip()
            if line and "@" in line:
                emails.append({"email": line, "created_at": ""})
    emails.reverse()
    return jsonify({"emails": emails, "count": len(emails)})


@app.route("/api/create", methods=["POST"])
def api_create():
    data = request.get_json() or {}
    count = min(int(data.get("count", 1)), 50)
    client = _icloud_client
    if not client:
        return jsonify({"ok": False, "error": "iCloud 客户端未初始化，请先导入 Cookie"})

    _update_state(creating=True)
    _emit_log("info", "手动创建 " + str(count) + " 个邮箱...")

    created, errors = [], []
    for i in range(count):
        try:
            result = client.create_alias(max_retries=3)
            email = result.get("email", "")
            if email:
                created.append(email)
                _emit_log("success", "[%d] %s" % (len(created), email))
                with open(str(RESULTS_DIR / "latest_emails.txt"), "a", encoding="utf-8") as fout:
                    fout.write(email + "\n")
            else:
                errors.append("empty")
                _emit_log("warn", "创建返回空结果")
        except Exception as e:
            err = str(e)
            errors.append(err)
            _emit_log("error", err[:120])
            if any(kw in err.lower() for kw in ["limit","exceeded","maximum","quota","429"]):
                _emit_log("warn", "触达上限，停止")
                break
            time.sleep(1)

    with _lock:
        _global_state["total_created"] += len(created)
        _global_state["today_created"] += len(created)
        _global_state["alias_count"] += len(created)
        _global_state["alias_active"] += len(created)
        _global_state["creating"] = False

    return jsonify({"ok": len(created) > 0, "emails": created, "count": len(created), "errors": errors})


@app.route("/api/set-cookies", methods=["POST"])
def api_set_cookies():
    data = request.get_json() or {}
    raw = (data.get("cookies") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "请提供 cookie 内容"})

    cookies = {}
    if raw.startswith("["):
        # Cookie Editor JSON 数组格式: [{"domain":...,"name":...,"value":...}, ...]
        try:
            arr = json.loads(raw)
            for item in arr:
                name = item.get("name", "").strip()
                value = item.get("value", "")
                if name:
                    cookies[name] = value
        except json.JSONDecodeError:
            return jsonify({"ok": False, "error": "JSON 数组格式无效"})
    elif raw.startswith("{"):
        try:
            p = json.loads(raw)
            if "cookie_header" in p:
                for part in p["cookie_header"].split("; "):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookies[k.strip()] = v.strip()
            else:
                cookies = p
        except json.JSONDecodeError:
            return jsonify({"ok": False, "error": "JSON 格式无效"})
    else:
        # Header String 格式: name1=value1; name2=value2
        for part in raw.split("; "):
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()

    if not cookies:
        return jsonify({"ok": False, "error": "未能解析出有效 cookie"})

    ok = init_icloud(cookies, host=data.get("host", "icloud.com"))
    if ok:
        _save_cookies(cookies)  # 持久化
        return jsonify({"ok": True, "count": len(cookies),
                        "alias_count": _global_state.get("alias_count", 0),
                        "alias_active": _global_state.get("alias_active", 0)})
    else:
        return jsonify({"ok": False, "error": _global_state.get("last_error", "校验失败")})


@app.route("/api/scheduler/start", methods=["POST"])
def api_scheduler_start():
    global _scheduler_thread, _stop_event
    if _scheduler_thread and _scheduler_thread.is_alive():
        return jsonify({"ok": False, "error": "调度器已在运行"})
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
    _scheduler_thread.start()
    _update_state(running=True)
    _emit_log("info", "调度器已启动")
    return jsonify({"ok": True})


@app.route("/api/scheduler/stop", methods=["POST"])
def api_scheduler_stop():
    global _stop_event
    _stop_event.set()
    _update_state(running=False, next_trigger=None)
    _emit_log("info", "调度器已停止")
    return jsonify({"ok": True})


@app.route("/api/log-stream")
def api_log_stream():
    def gen():
        while True:
            try:
                entry = _log_queue.get(timeout=25)
                yield "data: " + json.dumps(entry, ensure_ascii=False) + "\n\n"
            except queue.Empty:
                # 静默心跳，前端用注释行忽略
                yield ": heartbeat\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ============================================================
# Scheduler — 时间随机化 + 智能重试
# ============================================================

def _pick_next_trigger(after: datetime, first: bool = False) -> datetime:
    """选下一个触发时刻: 在 after 之后的下一个有效窗口内随机。
       first=True 时不要求 45min 间隔 (启动首轮)。"""
    earliest = after if first else after + timedelta(minutes=45)
    hour_base = earliest.replace(minute=0, second=0, microsecond=0)

    # 尝试: 后窗 (45-59) 是否有 >= earliest 的时刻
    back_end = hour_base.replace(minute=59, second=59, microsecond=999999)
    if back_end >= earliest:
        minute = max(45, earliest.minute if earliest.hour == hour_base.hour else 45)
        if minute <= 59:
            minute = minute + secrets.randbelow(60 - minute) if minute < 59 else 59
            return hour_base.replace(minute=minute, second=secrets.randbelow(60), microsecond=0)

    # 尝试: 前窗 (0-14) — 只有 earliest 在同一小时且 <=14 才可能
    if earliest.hour == hour_base.hour and earliest.minute <= 14:
        front_end = hour_base.replace(minute=14, second=59, microsecond=999999)
        if front_end >= earliest:
            minute = max(earliest.minute, 0)
            minute = minute + secrets.randbelow(15 - minute) if minute < 14 else 14
            return hour_base.replace(minute=minute, second=secrets.randbelow(60), microsecond=0)

    # 当前小时不行 → 下一小时，随机前/后窗
    hour_base += timedelta(hours=1)
    if secrets.randbelow(2) == 0:
        minute = secrets.randbelow(15)
    else:
        minute = 45 + secrets.randbelow(15)
    return hour_base.replace(minute=minute, second=secrets.randbelow(60), microsecond=0)


def _scheduler_loop():
    _emit_log("info", "调度器已启动 — 每小时随机触发, 间隔>45min, 连续2次上限延迟5m23s")
    consecutive_limit_failures = 0

    first_round = True
    while not _stop_event.is_set():
        # ---- 选下一触发时间 ----
        target = _pick_next_trigger(_now(), first=first_round)
        first_round = False
        _update_state(next_trigger=target.timestamp())
        _emit_log("info", "下次触发: %s" % target.strftime("%m/%d %H:%M:%S"))

        # ---- 等待到目标时间 ----
        while not _stop_event.is_set():
            rem = (target - _now()).total_seconds()
            if rem <= 0:
                break
            time.sleep(min(rem, 30))

        if _stop_event.is_set():
            break

        # ---- 执行一轮 ----
        try:
            hit_limit = _run_one_round()
        except Exception as e:
            _emit_log("error", "调度器异常 (自动恢复): %s" % str(e)[:150])
            _update_state(creating=False)
            hit_limit = False

        # ---- 连续2轮因上限失败 → 延迟5m23s重试 ----
        if hit_limit:
            consecutive_limit_failures += 1
        else:
            consecutive_limit_failures = 0

        if consecutive_limit_failures >= 2:
            _emit_log("warn", "连续2轮触发均因上限失败, 等待 5分23秒...")
            _update_state(round_status="冷却重试 (5m23s)")
            time.sleep(323)
            if not _stop_event.is_set():
                try:
                    _run_one_round()
                except Exception as e:
                    _emit_log("error", "重试异常: %s" % str(e)[:150])
                    _update_state(creating=False)
            consecutive_limit_failures = 0


def _run_one_round() -> bool:
    """执行一轮创建, 返回 True=因上限停止"""
    _update_state(creating=True, current_round_created=0, rate_limited=False, round_status="创建中...")
    _emit_log("info", "══════ 新一轮 ══════")

    hit_limit = False
    consecutive = 0
    while not _stop_event.is_set():
        try:
            cl = _icloud_client
            if not cl:
                _emit_log("error", "iCloud 客户端未初始化，请先导入 Cookie")
                _update_state(round_status="未导入 Cookie")
                break
            r = cl.create_alias(max_retries=3)
            email = r.get("email", "")
            if email:
                with _lock:
                    _global_state["total_created"] += 1
                    _global_state["today_created"] += 1
                    _global_state["current_round_created"] += 1
                    _global_state["alias_count"] += 1
                    _global_state["alias_active"] += 1
                n = _global_state["current_round_created"]
                _emit_log("success", "  #%d  %s" % (n, email))
                with open(str(RESULTS_DIR / "latest_emails.txt"), "a", encoding="utf-8") as fout:
                    fout.write(email + "\n")
                consecutive = 0
            else:
                consecutive += 1
        except Exception as e:
            err = str(e); consecutive += 1
            if _is_limit_error(err):
                _emit_log("warn", "  ⛔ 已达上限，本轮停止: %s" % err[:200])
                _update_state(rate_limited=True); hit_limit = True; break
            if any(kw in err.lower() for kw in ["401","403","cookie","session"]):
                _emit_log("error", "💀 Cookie 失效: %s" % err[:200])
                _update_state(cookies_ok=False, last_error="Cookie 已过期"); break
            _emit_log("warn", "  ⚠ %s" % err[:200])
        if consecutive >= 3:
            _emit_log("warn", "  ⛔ 连续 3 次失败，本轮停止")
            _update_state(rate_limited=True); hit_limit = True; break

    _update_state(creating=False)
    n = _global_state["current_round_created"]
    if hit_limit:
        _emit_log("info", "本轮结束: +%d 个 (已达上限)" % n)
        _update_state(round_status="已达上限 (+%d)" % n)
    elif n > 0:
        _emit_log("info", "本轮结束: +%d 个" % n)
        _update_state(round_status="本轮完成 (+%d)" % n)
    else:
        _emit_log("info", "本轮结束: 0 个")
        _update_state(round_status=_global_state.get("round_status") or "本轮 0 个")
    return hit_limit


# ============================================================
# Init
# ============================================================

def init_icloud(cookies, host="icloud.com"):
    global _icloud_client
    _icloud_client = ICloudHME(cookies, host=host, verbose=False)
    try:
        _icloud_client.validate_session()
        aliases = _icloud_client.list_aliases()
        active = sum(1 for a in aliases if a.get("active"))
        _update_state(cookies_ok=True, alias_count=len(aliases), last_error=None,
                      alias_active=active)
        _emit_log("success", "Cookie 有效 — %d 个别名 (活跃 %d)" % (len(aliases), active))
        return True
    except Exception as e:
        err = str(e)
        # 提取可读错误
        if "premiummailsettings" in err.lower() or "hide my email" in err.lower():
            hint = "未开通 iCloud+ 订阅 (Hide My Email 需要 iCloud+)"
        elif "401" in err or "403" in err:
            hint = "Cookie 已过期或无效，请重新导入"
        elif "timeout" in err.lower() or "connect" in err.lower():
            hint = "网络不通，请检查代理/VPN"
        else:
            hint = err[:120]
        _update_state(cookies_ok=False, last_error=hint)
        _emit_log("error", "Cookie 无效: " + hint)
        return False


def check_cookie_health():
    """后台定期校验 cookie 是否仍然有效"""
    global _icloud_client
    if not _icloud_client:
        return
    try:
        _icloud_client.validate_session()
        aliases = _icloud_client.list_aliases()
        active = sum(1 for a in aliases if a.get("active"))
        _update_state(cookies_ok=True, alias_count=len(aliases), last_error=None,
                      alias_active=active)
    except Exception as e:
        err = str(e)
        if "401" in err or "403" in err or "cookie" in err.lower() or "session" in err.lower():
            _update_state(cookies_ok=False, last_error="Cookie 已过期 — " + err[:80])
            _emit_log("error", "⚠️ Cookie 已过期! 请重新导入")


def _health_loop():
    """每 5 分钟校验 cookie 状态; 每 30 分钟校准时间"""
    ticks = 0
    while True:
        time.sleep(300)
        ticks += 1
        if _icloud_client:
            check_cookie_health()
        if ticks % 6 == 0:  # 每 30 分钟
            offset = _sync_time()
            if abs(offset) > 1:
                _emit_log("info", "时间校准: 偏移 %.1f 秒" % offset)


def main():
    import argparse
    p = argparse.ArgumentParser(description="iCloud HME Web UI")
    p.add_argument("--port", type=int, default=5050)
    p.add_argument("--host", type=str, default="0.0.0.0")
    p.add_argument("--cookies", type=str)
    p.add_argument("--scheduler", "-s", action="store_true")
    p.add_argument("--icloud-host", type=str, default="icloud.com")
    args = p.parse_args()

    cookies = {}
    if args.cookies:
        raw = open(args.cookies, "r", encoding="utf-8").read().strip()
        if not raw.startswith("{"):
            for part in raw.split("; "):
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies[k.strip()] = v.strip()
        else:
            data = json.loads(raw)
            if "cookie_header" in data:
                for part in data["cookie_header"].split("; "):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        cookies[k.strip()] = v.strip()
            else:
                cookies = data
        if cookies:
            print("[+] Loaded %d cookies from file" % len(cookies))
    elif _load_saved_cookies():
        cookies = _load_saved_cookies()
        print("[+] Loaded %d cookies from saved cookies.json" % len(cookies))
    else:
        try:
            cookies = extract_chrome_cookies()
            print("[+] Extracted %d cookies from Chrome" % len(cookies))
            _save_cookies(cookies)
        except Exception as e:
            print("[!] Cannot extract cookies: %s" % e)
            print("[*] Starting in offline mode — import cookies via Web UI")

    if cookies:
        init_icloud(cookies, host=args.icloud_host)
    else:
        _emit_log("warn", "No cookie loaded — use the Import button in the sidebar")

    # 联网校准时间
    offset = _sync_time()
    if abs(offset) > 0.5:
        print("[*] 时间校准: 偏移 %.1f 秒" % offset)

    # 后台健康检查: 每 5 分钟校验 cookie 是否过期
    threading.Thread(target=_health_loop, daemon=True).start()

    if args.scheduler:
        global _scheduler_thread, _stop_event
        _stop_event.clear()
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_thread.start()
        _update_state(running=True)
        print("[+] Scheduler auto-started")

    print("\n  Web UI -> http://%s:%s\n" % (args.host, args.port))
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
