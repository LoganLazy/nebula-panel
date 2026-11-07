"use strict";
var token = sessionStorage.getItem("nebula-token") || "";
var state = {servers:[], projects:[], last_inspect:{}};
var jobs = [];
var audit = [];
var jobDetails = {};
var expanded = {};
var editingServerId = null;
var editingProjectId = null;

function esc(value){
  return String(value == null ? "" : value).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c];});
}
function toast(message,error){
  var node=document.getElementById("toast");node.textContent=message;node.className="toast show"+(error?" error":"");
  setTimeout(function(){node.className="toast";},2800);
}
function fmtTime(value){
  if(!value)return "";
  try{return new Date(value).toLocaleString();}catch(e){return String(value);}
}
function showTokenPrompt(message){
  document.getElementById("token-prompt").classList.remove("hidden");
  var err=document.getElementById("token-error");
  err.textContent=message||"";
  document.getElementById("token-input").focus();
}
function hideTokenPrompt(){
  document.getElementById("token-prompt").classList.add("hidden");
  document.getElementById("token-error").textContent="";
}
async function api(path,options){
  options=options||{};options.headers=Object.assign({},options.headers||{},token?{"X-Nebula-Token":token}:{});
  var response=await fetch(path,options);var data=await response.json().catch(function(){return {};});
  if(response.status===403){var err=new Error(data.error||"请求令牌无效");err.auth=true;throw err;}
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
    var last=(state.last_inspect||{})[x.id];
    var inspectHtml=last?'<small>上次检查：'+esc(last.status==="success"?"成功":"失败")+' · '+esc(fmtTime(last.finished_at))+'</small><small>'+esc(last.snippet||"")+'</small>':"";
    return '<div class="item"><div><b>'+esc(x.name)+'</b><small>'+esc(x.user+"@"+x.host+":"+x.port)+'</small>'+inspectHtml+'</div><div class="item-actions"><button class="ghost mini inspect" data-id="'+x.id+'">检查</button><button class="ghost mini edit-server" data-id="'+x.id+'">编辑</button><button class="ghost mini danger remove-server" data-id="'+x.id+'">删除</button></div></div>';
  }).join(""):"暂无服务器";
  var projects=document.getElementById("project-list");
  projects.className="item-list"+(state.projects.length?"":" empty");
  projects.innerHTML=state.projects.length?state.projects.map(function(x){
    return '<div class="item"><div><b>'+esc(x.name)+'</b><small>'+esc(x.source_dir+" → "+x.remote_dir)+'</small></div><div class="item-actions"><button class="ghost mini edit-project" data-id="'+x.id+'">编辑</button><button class="ghost mini danger remove-project" data-id="'+x.id+'">删除</button></div></div>';
  }).join(""):"暂无项目";
  var serverSelect=document.getElementById("task-server"),projectSelect=document.getElementById("task-project"),serverValue=serverSelect.value,projectValue=projectSelect.value;
  serverSelect.innerHTML='<option value="">选择服务器</option>'+state.servers.map(function(x){return '<option value="'+x.id+'">'+esc(x.name)+'</option>';}).join("");serverSelect.value=serverValue;
  projectSelect.innerHTML='<option value="">选择项目</option>'+state.projects.map(function(x){return '<option value="'+x.id+'">'+esc(x.name)+'</option>';}).join("");projectSelect.value=projectValue;
  var list=document.getElementById("job-list");list.className="jobs"+(jobs.length?"":" empty");
  list.innerHTML=jobs.length?jobs.map(function(x){
    var detail=jobDetails[x.id];
    var isOpen=!!expanded[x.id]||x.status==="running"||x.status==="queued";
    var output=detail?detail.output:(isOpen?x.output_tail:x.output_tail);
    var plan=detail&&detail.plan?'<details class="plan-view" open><summary>审阅 AI 部署计划</summary><pre>'+esc(JSON.stringify(detail.plan,null,2))+'</pre></details>':"";
    var deploy=x.action==="plan"&&x.status==="success"?'<button class="primary mini deploy" data-plan="'+x.id+'">按此计划部署</button>':"";
    var retry=x.action==="apply"&&x.status==="failed"?'<button class="primary mini retry-apply" data-id="'+x.id+'">重试</button>':"";
    var replan=x.action==="plan"&&x.status==="failed"?'<button class="primary mini retry-plan" data-id="'+x.id+'">重新生成计划</button>':"";
    var cancel=(x.status==="running"||x.status==="queued")?'<button class="ghost mini danger cancel-job" data-id="'+x.id+'">取消</button>':"";
    var del=(x.status==="success"||x.status==="failed"||x.status==="cancelled"||x.status==="interrupted")?'<button class="ghost mini danger delete-job" data-id="'+x.id+'">删除</button>':"";
    var toggle='<button class="ghost mini toggle-job" data-id="'+x.id+'">'+(isOpen?"收起":"展开日志")+'</button>';
    return '<article class="job"><div class="job-summary"><div><b>'+esc(x.action.toUpperCase()+" · "+x.server_name)+'</b><span> '+esc(x.project_name||"服务器检查")+'</span></div><div class="item-actions">'+toggle+cancel+deploy+retry+replan+del+'<span class="job-status '+esc(x.status)+'">'+esc(statusName(x.status))+'</span></div></div>'+plan+'<pre>'+esc(output||"等待任务输出…")+'</pre></article>';
  }).join(""):"还没有执行记录";
  var auditList=document.getElementById("audit-list");
  var recent=audit.slice(0,20);
  auditList.className="audit-list"+(recent.length?"":" empty");
  auditList.innerHTML=recent.length?recent.map(function(e){
    return '<div class="audit-item"><span>'+esc(fmtTime(e.ts))+'</span><b>'+esc(e.action)+'</b><span>'+esc(e.target||"")+'</span></div>';
  }).join(""):"暂无操作记录";
}
async function reload(){
  try{
    var all=await Promise.all([api("/api/state"),api("/api/jobs"),api("/api/audit")]);
    state=all[0];jobs=all[1];audit=all[2];
    var need=jobs.filter(function(x){return x.status==="running"||x.status==="queued"||expanded[x.id];});
    for(var i=0;i<need.length;i++){
      try{jobDetails[need[i].id]=await api("/api/jobs/"+need[i].id);}catch(e){/* 详情拉不到就用摘要里的末尾片段 */}
    }
    render();
  }catch(e){if(e.auth){showTokenPrompt("令牌无效，请重新输入");}else{toast(e.message,true);}}
}
async function create(kind,body){
  await api("/api/"+kind,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});await reload();toast("已保存");
}
async function update(kind,id,body){
  await api("/api/"+kind+"/"+id,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});await reload();toast("已保存");
}
async function remove(kind,id){
  if(!confirm("确认删除这条配置？"))return;
  await api("/api/"+kind+"/"+id,{method:"DELETE",headers:{"Content-Type":"application/json"},body:"{}"});await reload();toast("已删除");
}
async function job(body){
  await api("/api/jobs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});await reload();toast("任务已进入队列");
}
function setServerForm(x){
  var form=document.getElementById("server-form");
  form.name.value=x?x.name:"";form.host.value=x?x.host:"";form.user.value=x?x.user:"deploy";
  form.port.value=x?x.port:22;form.identity_file.value=x?x.identity_file:"";
  document.getElementById("server-submit").textContent=x?"保存修改":"添加服务器";
  document.getElementById("server-edit-cancel").classList.toggle("hidden",!x);
}
function setProjectForm(x){
  var form=document.getElementById("project-form");
  form.name.value=x?x.name:"";form.max_source_mb.value=x?x.max_source_mb:512;
  form.source_dir.value=x?x.source_dir:"";form.remote_dir.value=x?x.remote_dir:"";
  form.ai_base_url.value=x?x.ai_base_url:"";form.ai_model.value=x?x.ai_model:"";
  document.getElementById("project-submit").textContent=x?"保存修改":"添加项目";
  document.getElementById("project-edit-cancel").classList.toggle("hidden",!x);
}

document.getElementById("server-form").addEventListener("submit",async function(e){
  e.preventDefault();try{var b=formData(e.target);b.port=Number(b.port);
    if(editingServerId){await update("servers",editingServerId,b);}else{await create("servers",b);}
    editingServerId=null;e.target.reset();setServerForm(null);
  }catch(x){if(x.auth){showTokenPrompt("令牌无效，请重新输入");}else{toast(x.message,true);}}
});
document.getElementById("project-form").addEventListener("submit",async function(e){
  e.preventDefault();try{var b=formData(e.target);b.max_source_mb=Number(b.max_source_mb);
    if(editingProjectId){await update("projects",editingProjectId,b);}else{await create("projects",b);}
    editingProjectId=null;e.target.reset();setProjectForm(null);
  }catch(x){if(x.auth){showTokenPrompt("令牌无效，请重新输入");}else{toast(x.message,true);}}
});
document.getElementById("server-edit-cancel").addEventListener("click",function(){editingServerId=null;document.getElementById("server-form").reset();setServerForm(null);});
document.getElementById("project-edit-cancel").addEventListener("click",function(){editingProjectId=null;document.getElementById("project-form").reset();setProjectForm(null);});
document.getElementById("task-form").addEventListener("submit",async function(e){
  e.preventDefault();var action=e.submitter.value,b=formData(e.target);b.action=action;
  if(action==="plan"&&!b.project_id)return toast("生成计划需要选择项目",true);
  try{await job(b);}catch(x){if(x.auth){showTokenPrompt("令牌无效，请重新输入");}else{toast(x.message,true);}}
});
document.body.addEventListener("click",async function(e){
  var target=e.target;
  try{
    if(target.classList.contains("remove-server"))await remove("servers",target.dataset.id);
    if(target.classList.contains("remove-project"))await remove("projects",target.dataset.id);
    if(target.classList.contains("edit-server")){
      var s=state.servers.find(function(x){return x.id===target.dataset.id;});
      if(s){editingServerId=s.id;setServerForm(s);document.getElementById("server-form").scrollIntoView();}
    }
    if(target.classList.contains("edit-project")){
      var p=state.projects.find(function(x){return x.id===target.dataset.id;});
      if(p){editingProjectId=p.id;setProjectForm(p);document.getElementById("project-form").scrollIntoView();}
    }
    if(target.classList.contains("inspect"))await job({action:"inspect",server_id:target.dataset.id});
    if(target.classList.contains("toggle-job")){
      expanded[target.dataset.id]=!expanded[target.dataset.id];render();
      if(expanded[target.dataset.id]){try{jobDetails[target.dataset.id]=await api("/api/jobs/"+target.dataset.id);}catch(err){}render();}
    }
    if(target.classList.contains("delete-job")){
      if(confirm("确认删除这条任务记录？")){await api("/api/jobs/"+target.dataset.id,{method:"DELETE",headers:{"Content-Type":"application/json"},body:"{}"});delete jobDetails[target.dataset.id];delete expanded[target.dataset.id];await reload();toast("已删除");}
    }
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
    if(target.classList.contains("retry-apply")){
      var failed=jobs.find(function(x){return x.id===target.dataset.id;});
      var server2=state.servers.find(function(x){return x.id===failed.server_id;});
      var project2=state.projects.find(function(x){return x.id===failed.project_id;});
      if(!server2||!project2)throw new Error("服务器或项目配置已不存在");
      if(!confirm("用同一个计划重试部署，确认继续？"))return;
      await job({action:"apply",server_id:server2.id,project_id:project2.id,plan_job_id:failed.plan_job_id,retry_of:failed.id,allow_swap:document.getElementById("deploy-swap").checked,auto_repair:Number(document.getElementById("deploy-retries").value)});
    }
    if(target.classList.contains("retry-plan")){
      var failedPlan=jobs.find(function(x){return x.id===target.dataset.id;});
      if(!state.servers.find(function(x){return x.id===failedPlan.server_id;})||!state.projects.find(function(x){return x.id===failedPlan.project_id;}))throw new Error("服务器或项目配置已不存在");
      await job({action:"plan",server_id:failedPlan.server_id,project_id:failedPlan.project_id,retry_of:failedPlan.id});
    }
    if(target.id==="refresh")await reload();
  }catch(x){if(x.auth){showTokenPrompt("令牌无效，请重新输入");}else{toast(x.message,true);}}
});
document.getElementById("token-submit").addEventListener("click",async function(){
  var value=document.getElementById("token-input").value.trim();
  if(!value)return showTokenPrompt("请先输入令牌");
  token=value;
  try{
    var session=await api("/api/session");
    sessionStorage.setItem("nebula-token",token);
    hideTokenPrompt();
    var deploy=document.getElementById("deploy-status");deploy.textContent=session.deploy_ready?"Deploy 已连接":"Deploy 未找到";deploy.classList.add(session.deploy_ready?"ok":"warn");
    var ai=document.getElementById("ai-status");ai.textContent=session.ai_key_ready?"AI 密钥已加载":"AI 密钥未加载";ai.classList.add(session.ai_key_ready?"ok":"warn");
    await reload();setInterval(reload,2000);
  }catch(e){token="";sessionStorage.removeItem("nebula-token");showTokenPrompt("令牌无效，请重新输入");}
});

(async function(){
  if(!token){showTokenPrompt();return;}
  try{
    var session=await api("/api/session");
    var deploy=document.getElementById("deploy-status");deploy.textContent=session.deploy_ready?"Deploy 已连接":"Deploy 未找到";deploy.classList.add(session.deploy_ready?"ok":"warn");
    var ai=document.getElementById("ai-status");ai.textContent=session.ai_key_ready?"AI 密钥已加载":"AI 密钥未加载";ai.classList.add(session.ai_key_ready?"ok":"warn");
    await reload();setInterval(reload,2000);
  }catch(e){token="";sessionStorage.removeItem("nebula-token");showTokenPrompt("令牌无效，请重新输入");}
})();
