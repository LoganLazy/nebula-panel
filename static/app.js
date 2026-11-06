"use strict";
var token = "";
var state = {servers:[], projects:[]};
var jobs = [];

function esc(value){
  return String(value == null ? "" : value).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c];});
}
function toast(message,error){
  var node=document.getElementById("toast");node.textContent=message;node.className="toast show"+(error?" error":"");
  setTimeout(function(){node.className="toast";},2800);
}
async function api(path,options){
  options=options||{};options.headers=Object.assign({},options.headers||{},token?{"X-Nebula-Token":token}:{});
  var response=await fetch(path,options);var data=await response.json();
  if(!response.ok)throw new Error(data.error||"请求失败");return data;
}
function formData(form){var output={};new FormData(form).forEach(function(v,k){output[k]=v;});return output;}
function statusName(value){return {queued:"等待",running:"运行中",cancelling:"取消中",cancelled:"已取消",interrupted:"已中断",success:"成功",failed:"失败"}[value]||value;}

function render(){
  document.getElementById("server-count").textContent=state.servers.length;
  document.getElementById("project-count").textContent=state.projects.length;
  document.getElementById("running-count").textContent=jobs.filter(function(x){return x.status==="running"||x.status==="queued";}).length;
  var servers=document.getElementById("server-list");
  servers.className="item-list"+(state.servers.length?"":" empty");
  servers.innerHTML=state.servers.length?state.servers.map(function(x){
    return '<div class="item"><div><b>'+esc(x.name)+'</b><small>'+esc(x.user+"@"+x.host+":"+x.port)+'</small></div><div class="item-actions"><button class="ghost mini inspect" data-id="'+x.id+'">检查</button><button class="ghost mini danger remove-server" data-id="'+x.id+'">删除</button></div></div>';
  }).join(""):"暂无服务器";
  var projects=document.getElementById("project-list");
  projects.className="item-list"+(state.projects.length?"":" empty");
  projects.innerHTML=state.projects.length?state.projects.map(function(x){
    return '<div class="item"><div><b>'+esc(x.name)+'</b><small>'+esc(x.source_dir+" → "+x.remote_dir)+'</small></div><button class="ghost mini danger remove-project" data-id="'+x.id+'">删除</button></div>';
  }).join(""):"暂无项目";
  var serverSelect=document.getElementById("task-server"),projectSelect=document.getElementById("task-project"),serverValue=serverSelect.value,projectValue=projectSelect.value;
  serverSelect.innerHTML='<option value="">选择服务器</option>'+state.servers.map(function(x){return '<option value="'+x.id+'">'+esc(x.name)+'</option>';}).join("");serverSelect.value=serverValue;
  projectSelect.innerHTML='<option value="">选择项目</option>'+state.projects.map(function(x){return '<option value="'+x.id+'">'+esc(x.name)+'</option>';}).join("");projectSelect.value=projectValue;
  var list=document.getElementById("job-list");list.className="jobs"+(jobs.length?"":" empty");
  list.innerHTML=jobs.length?jobs.map(function(x){
    var deploy=x.action==="plan"&&x.status==="success"?'<button class="primary mini deploy" data-plan="'+x.id+'">按此计划部署</button>':"";
    var cancel=(x.status==="running"||x.status==="queued")?'<button class="ghost mini danger cancel-job" data-id="'+x.id+'">取消</button>':"";
    var plan=x.plan?'<details class="plan-view"><summary>审阅 AI 部署计划</summary><pre>'+esc(JSON.stringify(x.plan,null,2))+'</pre></details>':"";
    return '<article class="job"><div class="job-summary"><div><b>'+esc(x.action.toUpperCase()+" · "+x.server_name)+'</b><span> '+esc(x.project_name||"服务器检查")+'</span></div><div class="item-actions">'+cancel+deploy+'<span class="job-status '+esc(x.status)+'">'+esc(statusName(x.status))+'</span></div></div>'+plan+'<pre>'+esc(x.output||"等待任务输出…")+'</pre></article>';
  }).join(""):"还没有执行记录";
}
async function reload(){
  try{var all=await Promise.all([api("/api/state"),api("/api/jobs")]);state=all[0];jobs=all[1];render();}catch(e){toast(e.message,true);}
}
async function create(kind,body){
  await api("/api/"+kind,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});await reload();toast("已保存");
}
async function remove(kind,id){
  if(!confirm("确认删除这条配置？"))return;
  await api("/api/"+kind+"/"+id,{method:"DELETE",headers:{"Content-Type":"application/json"},body:"{}"});await reload();toast("已删除");
}
async function job(body){
  await api("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});await reload();toast("任务已进入队列");
}

document.getElementById("server-form").addEventListener("submit",async function(e){
  e.preventDefault();try{var b=formData(e.target);b.port=Number(b.port);await create("servers",b);e.target.reset();}catch(x){toast(x.message,true);}
});
document.getElementById("project-form").addEventListener("submit",async function(e){
  e.preventDefault();try{var b=formData(e.target);b.max_source_mb=Number(b.max_source_mb);await create("projects",b);e.target.reset();}catch(x){toast(x.message,true);}
});
document.getElementById("task-form").addEventListener("submit",async function(e){
  e.preventDefault();var action=e.submitter.value,b=formData(e.target);b.action=action;
  if(action==="plan"&&!b.project_id)return toast("生成计划需要选择项目",true);
  try{await job(b);}catch(x){toast(x.message,true);}
});
document.body.addEventListener("click",async function(e){
  var target=e.target;
  try{
    if(target.classList.contains("remove-server"))await remove("servers",target.dataset.id);
    if(target.classList.contains("remove-project"))await remove("projects",target.dataset.id);
    if(target.classList.contains("inspect"))await job({action:"inspect",server_id:target.dataset.id});
    if(target.classList.contains("cancel-job")){
      if(confirm("确认取消这个运行任务？")){await api("/api/jobs/"+target.dataset.id+"/cancel",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});await reload();}
    }
    if(target.classList.contains("deploy")){
      var plan=jobs.find(function(x){return x.id===target.dataset.plan;});
      var server=state.servers.find(function(x){return x.id===plan.server_id;});
      var project=state.projects.find(function(x){return x.id===plan.project_id;});
      if(!server||!project)throw new Error("服务器或项目配置已不存在");
      if(!confirm("将按照该 AI 计划上传源码并修改服务器，确认继续？"))return;
      await job({action:"apply",server_id:server.id,project_id:project.id,plan_job_id:plan.id,allow_swap:document.getElementById("deploy-swap").checked,auto_repair:Number(document.getElementById("deploy-retries").value)});
    }
    if(target.id==="refresh")await reload();
  }catch(x){toast(x.message,true);}
});

(async function(){
  try{
    var session=await api("/api/session");token=session.token;
    var deploy=document.getElementById("deploy-status");deploy.textContent=session.deploy_ready?"Deploy 已连接":"Deploy 未找到";deploy.classList.add(session.deploy_ready?"ok":"warn");
    var ai=document.getElementById("ai-status");ai.textContent=session.ai_key_ready?"AI 密钥已加载":"AI 密钥未加载";ai.classList.add(session.ai_key_ready?"ok":"warn");
    await reload();setInterval(reload,2000);
  }catch(e){toast(e.message,true);}
})();
