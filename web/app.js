const $ = (id) => document.getElementById(id);
const phases = [
  {id:'brief',label:'需求访谈',intro:'我们会根据你的回答一步步澄清问题、用户、使用流程和首版范围；准备好后再整理 PRD。'},
  {id:'components',label:'组件确认',intro:'根据需求推荐最小 Agent 能力组件。逐项选择后再确认，UI 页面组件不会混在这里。'},
  {id:'architecture',label:'架构方案',intro:'查看分层架构、工具清单、模型接入和安全边界。确认方案后才会进入代码开发。'},
  {id:'development',label:'开发任务',intro:'先确定本次生成范围，再查看前后端任务及各自的验收条件。'},
  {id:'ui_design',label:'页面设计确认',intro:'如果本次包含前端，先审阅页面清单、布局、交互状态和窄屏设计。确认后才生成前端代码。'},
  {id:'frontend',label:'前端开发',intro:'前端工作包含页面结构、组件、交互状态、真实接口接入和独立验证。'},
  {id:'backend',label:'后端开发',intro:'后端工作包含服务接口、会话持久化、任务状态、模型调用和异常处理。'},
  {id:'verification',label:'联调验收',intro:'汇总前后端运行情况、完整流程验证、验收结果和遗留问题。'},
];
const deliverables = [
  {id:'requirements',label:'需求文档',phase:'brief'},
  {id:'components',label:'组件清单',phase:'components'},
  {id:'architecture',label:'架构方案',phase:'architecture'},
  {id:'development',label:'任务拆分',phase:'development'},
  {id:'ui_design',label:'页面设计',phase:'ui_design'},
  {id:'frontend',label:'前端成果',phase:'frontend'},
  {id:'backend',label:'后端成果',phase:'backend'},
  {id:'verification',label:'验收结果',phase:'verification'},
];
const componentOptions = [
  ['tools','工具系统','接入外部系统、文件、API 或数据源'],
  ['permissions','权限系统','执行写入、命令或其他敏感操作前需要审批'],
  ['hooks','钩子系统','需要在工具调用前后拦截、记录或审计'],
  ['planning','任务规划','包含多个需要跟踪的步骤'],
  ['subagents','子 Agent','任务可并行或需要隔离上下文'],
  ['skills','技能加载','领域知识较多，需要按需加载'],
  ['compaction','上下文压缩','对话或工具结果可能超出上下文'],
  ['memory','记忆系统','跨会话保留用户偏好或产品决策'],
  ['tasks','持久化任务','需要断点续跑或任务依赖'],
  ['background','后台任务','慢操作不能阻塞主交互'],
  ['scheduler','定时调度','需要按计划自动触发任务'],
  ['teams','Agent 团队','多 Agent 并行工作及工作区隔离'],
  ['mcp','MCP 插件','接入 MCP 工具生态'],
  ['workflow','工作流运行时','流程固定，需跟踪或恢复'],
];
let session = null;
let config = {configured:false,providers:[]};
let draftModel = null;
let editingProviderId = '';
let activeDeliverable = 'requirements';
let pendingJob = null;
let sessions = [];
let attachedFile = '';
let entryMode = 'idea';
let renamingProjectId = null;
let renameDraft = '';
let renameError = '';
let confirmingDeleteProjectId = null;
let deleteError = '';
let feishuImportGeneration = 0;
const importedFeishuUrls = new Set();
let feishuAuthFlowId = null;

function escapeHtml(value='') { return String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function request(path, options={}) {
  const response = await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});
  const data = await response.json().catch(()=>({error:'服务器返回了无法读取的响应。'}));
  if (!response.ok) {const error=new Error(data.error||`请求失败 (${response.status})`);error.authRequired=Boolean(data.auth_required);throw error;}
  return data;
}
function modelValue(selection) {return selection ? `${encodeURIComponent(selection.provider_id)}|${encodeURIComponent(selection.model)}` : '';}
function parseModel(value) {if(!value)return null;const [provider_id,model]=value.split('|').map(decodeURIComponent);return {provider_id,model};}
function renderModelSelector() {
  const selected=session ? session.model_selection : draftModel;
  const value=modelValue(selected);
  let options='<option value="">请选择模型</option>';
  for(const p of config.providers||[]) options+=`<optgroup label="${escapeHtml(p.name)}">${p.models.map(m=>`<option value="${escapeHtml(modelValue({provider_id:p.id,model:m}))}">${escapeHtml(m)}</option>`).join('')}</optgroup>`;
  $('modelBadge').innerHTML=options;
  $('modelBadge').value=value;
  if(value&&!$('modelBadge').value){$('modelBadge').value='';if(!session)draftModel=null;}
}
async function refreshHealth() {
  try { config=await request('/api/health'); }
  catch { config={configured:false,providers:[]}; }
  renderModelSelector();
  $('configDot').classList.toggle('configured',config.configured);
}
async function refreshSessions() {
  try { const data=await request('/api/sessions'); sessions=data.sessions||[]; }
  catch { sessions=[]; }
  $('projectList').innerHTML=sessions.length?sessions.map(s=>{
    const id=escapeHtml(s.id), title=escapeHtml(s.title||'新建产品项目');
    if(s.id===renamingProjectId)return `<div class="project-edit-wrap"><div class="project-row project-row-editing"><input class="project-rename-input" data-project-rename-input="${id}" aria-label="项目名称" value="${escapeHtml(renameDraft)}" maxlength="80"><button class="project-action project-save" data-save-rename="${id}" aria-label="保存项目名称" title="回车保存">↵</button><button class="project-action" data-cancel-rename="${id}" aria-label="取消重命名" title="取消">×</button></div>${renameError?`<small class="project-rename-error">${escapeHtml(renameError)}</small>`:''}</div>`;
    return `<div class="project-item"><div class="project-row"><button class="project-link ${session?.id===s.id?'active':''}" data-session="${id}" title="${title}">${title}</button><button class="project-action" data-rename="${id}" aria-label="重命名项目" title="重命名">✎</button><button class="project-action project-delete" data-delete="${id}" aria-label="删除项目" title="删除">×</button></div>${s.id===confirmingDeleteProjectId?`<div class="project-confirm"><span>确定删除“${title}”？项目对话和生成成果将一并删除。</span>${deleteError?`<small>${escapeHtml(deleteError)}</small>`:''}<div class="project-confirm-actions"><button data-cancel-delete="${id}">取消</button><button class="confirm-delete-button" data-confirm-delete="${id}">删除</button></div></div>`:''}</div>`;
  }).join(''):'<div class="sidebar-empty">还没有项目</div>';
  $('mobileProjectSelect').innerHTML='<option value="">项目列表</option>'+sessions.map(s=>`<option value="${escapeHtml(s.id)}" ${session?.id===s.id?'selected':''}>${escapeHtml(s.title||'新建产品项目')}</option>`).join('');
  document.querySelectorAll('[data-session]').forEach(el=>el.addEventListener('click',()=>openSession(el.dataset.session)));
  document.querySelectorAll('[data-rename]').forEach(el=>el.addEventListener('click',()=>beginRenameProject(el.dataset.rename)));
  document.querySelectorAll('[data-project-rename-input]').forEach(el=>{
    el.addEventListener('input',()=>{renameDraft=el.value});
    el.addEventListener('keydown',e=>{
      if(e.key==='Enter'){e.preventDefault();saveProjectRename(el.dataset.projectRenameInput)}
      if(e.key==='Escape'){e.preventDefault();cancelProjectRename()}
    });
  });
  document.querySelectorAll('[data-save-rename]').forEach(el=>el.addEventListener('click',()=>saveProjectRename(el.dataset.saveRename)));
  document.querySelectorAll('[data-cancel-rename]').forEach(el=>el.addEventListener('click',cancelProjectRename));
  document.querySelectorAll('[data-delete]').forEach(el=>el.addEventListener('click',()=>showDeleteConfirmation(el.dataset.delete)));
  document.querySelectorAll('[data-cancel-delete]').forEach(el=>el.addEventListener('click',cancelDeleteConfirmation));
  document.querySelectorAll('[data-confirm-delete]').forEach(el=>el.addEventListener('click',()=>deleteProject(el.dataset.confirmDelete)));
  const renameInput=document.querySelector('[data-project-rename-input]');
  if(renameInput){renameInput.focus();renameInput.select()}
}
async function beginRenameProject(id) {
  const project=sessions.find(item=>item.id===id); if(!project)return;
  renamingProjectId=id;
  renameDraft=project.title||'新建产品项目';
  renameError='';
  await refreshSessions();
}
function cancelProjectRename() {
  renamingProjectId=null;
  renameDraft='';
  renameError='';
  refreshSessions();
}
async function saveProjectRename(id) {
  const title=renameDraft.trim();
  if(!title){renameError='项目名称不能为空';await refreshSessions();return;}
  try {
    const data=await request(`/api/sessions/${encodeURIComponent(id)}/rename`,{method:'POST',body:JSON.stringify({title})});
    if(session?.id===id)session=data.session;
    renamingProjectId=null;
    renameDraft='';
    renameError='';
    await refreshSessions();
    if(session?.id===id)$('projectTitle').textContent=session.title;
  } catch(e) { renameError=e.message;await refreshSessions(); }
}
function showDeleteConfirmation(id) {
  confirmingDeleteProjectId=id;
  deleteError='';
  renamingProjectId=null;
  renameDraft='';
  renameError='';
  refreshSessions();
}
function cancelDeleteConfirmation() {
  confirmingDeleteProjectId=null;
  deleteError='';
  refreshSessions();
}
async function deleteProject(id) {
  const project=sessions.find(item=>item.id===id); if(!project)return;
  try {
    await request(`/api/sessions/${encodeURIComponent(id)}/delete`,{method:'POST',body:JSON.stringify({})});
    confirmingDeleteProjectId=null;
    deleteError='';
    if(session?.id===id){session=null;pendingJob=null;activeDeliverable='requirements';resetNewProjectForm();renderWorkspace();}
    await refreshSessions();
  } catch(e) { deleteError=e.message;await refreshSessions(); }
}
function currentIndex() { return Math.max(0,phases.findIndex(p=>p.id===session?.phase)); }
function renderWorkflow() {
  const current=currentIndex();
  $('workflowBar').innerHTML=phases.map((p,i)=>`<button class="workflow-step ${i===current?'active':i<current?'done':''}" data-workflow="${p.id}" ${i<current&&session?.approvals?.[p.id]?'':'disabled'}><span class="step-num">${i<current?'✓':i+1}</span><span>${p.label}</span></button>`).join('');
  document.querySelectorAll('[data-workflow]').forEach(b=>b.addEventListener('click',()=>{
    if(!session?.approvals?.[b.dataset.workflow])return;
    session.phase=b.dataset.workflow;activeDeliverable=b.dataset.workflow==='brief'?'requirements':b.dataset.workflow;renderWorkspace();
  }));
}
function renderMessages() {
  const messages=session?.messages||[];
  $('messages').innerHTML=messages.map((m,i)=>{
    const incomplete=i===messages.length-1&&m.role==='assistant'&&session?.phase!=='brief'&&session?.status!=='running'&&/(?:^|\n)\s*#{1,6}\s*(?:\d+[.)]?\s*)?$/.test(m.content);
    return `<article class="message ${m.role==='user'?'user':'assistant'}">${m.role==='assistant'?'<div class="message-label">NEXUS</div>':''}<div class="message-content">${escapeHtml(m.content)}</div>${incomplete?'<button class="continue-answer" data-continue-answer>继续生成剩余内容 →</button>':''}</article>`;
  }).join('');
  $('messages').querySelector('[data-continue-answer]')?.addEventListener('click',continueAnswer);
  $('messages').scrollTop=$('messages').scrollHeight;
}
function resultFor(phase) { return session?.phase_results?.[phase]; }
function renderComponents() {
  const selected=new Set(session?.selected_components||[]);
  $('componentChoices').innerHTML=componentOptions.map(([id,label,description])=>`<label class="component-choice"><input type="checkbox" value="${id}" ${selected.has(id)?'checked':''}><span><b>${label}</b>${description}</span></label>`).join('');
}
function renderControls() {
  const phase=session?.phase||'brief'; const approved=session?.approvals||{}; const result=resultFor(phase); let html='';
  if (session?.status==='running') { $('phaseControls').innerHTML=''; return; }
  if (phase==='brief') {
    const discovery=session.discovery||{};
    if(discovery.status==='draft') html='<button class="approve-button" data-approve="brief">确认 PRD，进入组件确认 <span>→</span></button><button class="secondary-button" data-redraft>继续修改需求</button>';
    else if(!discovery.started) html='<button class="run-button" data-discovery-start>开始需求访谈 <span>→</span></button>';
    else html=`<button class="approve-button" data-prd-draft ${discovery.can_draft?'':'disabled'}>整理 PRD 草稿 <span>→</span></button><span class="phase-hint">${discovery.can_draft?'核心信息已具备，可以整理草稿。':'回答访谈问题后，逐步明确需求。'}</span>`;
  }
  if (phase==='components') {
    if (!result) html='<button class="run-button" data-run="components">获取组件建议 <span>↗</span></button>';
    else html=`<div class="component-choices" id="componentChoices"></div><button class="approve-button" data-approve="components">确认选中组件 <span>→</span></button><button class="secondary-button" data-run="components">刷新建议</button>`;
  }
  if (phase==='architecture') html=result?'<button class="approve-button" data-approve="architecture">确认架构方案 <span>→</span></button><button class="secondary-button" data-run="architecture">重新生成方案</button>':'<button class="run-button" data-run="architecture">生成架构方案 <span>↗</span></button>';
  if (phase==='development') {
    if (result) html=`<select class="scope-select" id="generationScope"><option value="fullstack">前端 + 后端</option><option value="frontend">仅前端</option><option value="backend">仅后端</option></select><button class="approve-button" data-approve="development">确认范围并开始开发 <span>→</span></button><button class="secondary-button" data-run="development">重新拆分任务</button>`;
    else html='<button class="run-button" data-run="development">先生成开发任务 <span>↗</span></button>';
  }
  if (phase==='ui_design') html=result?'<button class="approve-button" data-approve="ui_design">确认页面设计，开始前端开发 <span>→</span></button><button class="secondary-button" data-run="ui_design">重新生成设计说明</button>':'<button class="run-button" data-run="ui_design">生成页面设计说明 <span>↗</span></button>';
  if (phase==='frontend'||phase==='backend') {
    const other=phase==='frontend'?'backend':'frontend';
    html=`<button class="run-button" data-run="${phase}">生成${phase==='frontend'?'前端':'后端'}代码 <span>↗</span></button><button class="secondary-button" data-phase="${other}">切换到${other==='frontend'?'前端':'后端'}任务</button>`;
  }
  if (phase==='verification') html=result?'<button class="run-button" data-run="verification">重新验收 <span>↻</span></button>':'<button class="run-button" data-run="verification">开始联调验收 <span>↗</span></button>';
  $('phaseControls').innerHTML=html;
  if (phase==='components'&&result) renderComponents();
  document.querySelectorAll('[data-run]').forEach(b=>b.addEventListener('click',()=>runPhase(b.dataset.run)));
  document.querySelectorAll('[data-approve]').forEach(b=>b.addEventListener('click',()=>approvePhase(b.dataset.approve)));
  document.querySelectorAll('[data-phase]').forEach(b=>b.addEventListener('click',()=>changePhase(b.dataset.phase)));
  document.querySelector('[data-discovery-start]')?.addEventListener('click',startDiscovery);
  document.querySelector('[data-prd-draft]')?.addEventListener('click',createPrdDraft);
  document.querySelector('[data-redraft]')?.addEventListener('click',reopenDiscovery);
  if ($('generationScope')) $('generationScope').value=session.generation_scope||'fullstack';
}
function renderDiscovery() {
  const panel=$('discoverySummary');
  if(session?.phase!=='brief'){panel.classList.add('hidden');return;}
  const discovery=session.discovery||{};
  panel.classList.remove('hidden');
  const labels={target_users:'目标用户',problem:'要解决的问题',workflow:'核心使用流程',mvp_scope:'首版范围',success_criteria:'成功标准'};
  const facts=discovery.facts||{};
  const factRows=Object.entries(labels).map(([key,label])=>`<div class="discovery-fact"><span>${label}</span><b>${escapeHtml(facts[key]?.value||'待了解')}</b></div>`).join('');
  const questions=(discovery.open_questions||[]).map(q=>`<li>${escapeHtml(q)}</li>`).join('');
  const stages={direction:'讲清想法',value:'明确价值',workflow:'梳理流程',scope:'收敛范围',constraints:'补充约束'};
  panel.innerHTML=`<div class="discovery-heading"><b>需求访谈</b><span>${stages[discovery.stage]||'准备开始'}</span></div><div class="discovery-facts">${factRows}</div>${questions?`<details class="discovery-questions"><summary>待确认的问题（${(discovery.open_questions||[]).length}）</summary><ul>${questions}</ul></details>`:''}`;
}
function renderRight() {
  $('scopeCard').classList.toggle('hidden',!session?.approvals?.development);
  const scopes={fullstack:'前端 + 后端',frontend:'仅前端',backend:'仅后端'};
  $('scopeLabel').textContent=scopes[session?.generation_scope]||'尚未确定';
  const scope=session?.generation_scope||'fullstack';
  const taskPhases=['brief','components','architecture','development',...(scope==='backend'?[]:['ui_design','frontend']),...(scope==='frontend'?[]:['backend']),'verification'];
  $('tasksList').innerHTML=taskPhases.map(id=>{const done=Boolean(resultFor(id));const running=session?.phase===id&&session?.status==='running';const label=phases.find(p=>p.id===id)?.label||id;return `<div class="task-state"><span class="task-indicator ${done?'':running?'running':'pending'}"></span><span>${label}</span><small>${done?'已完成':running?'进行中':'待开始'}</small></div>`}).join('');
  $('deliverableTabs').innerHTML=deliverables.map(d=>`<button class="${activeDeliverable===d.id?'active':''}" data-deliverable="${d.id}">${d.label}</button>`).join('');
  document.querySelectorAll('[data-deliverable]').forEach(b=>b.addEventListener('click',()=>{activeDeliverable=b.dataset.deliverable;renderRight()}));
  const data=resultFor(activeDeliverable==='requirements'?'brief':activeDeliverable);
  const artifacts=data?.artifacts||[];
  $('fileCount').textContent=String(Object.values(session?.phase_results||{}).reduce((n,r)=>n+(r.artifacts||[]).length,0));
  const content=[];
  if(data?.text) content.push(`<article class="artifact-card"><b>${deliverables.find(d=>d.id===activeDeliverable)?.label||'阶段成果'}</b><small>${escapeHtml(data.phase||'')} · ${escapeHtml(new Date().toLocaleDateString('zh-CN'))}</small><p>${escapeHtml(data.text)}</p></article>`);
  for(const file of artifacts) content.push(`<article class="artifact-card"><b>${escapeHtml(file)}</b><small>本地生成成果</small><a href="/api/sessions/${encodeURIComponent(session.id)}/file?path=${encodeURIComponent(file)}" download>下载文件 ↓</a></article>`);
  if(['frontend','backend'].includes(activeDeliverable)&&artifacts.some(file=>!file.endsWith('.md')))content.unshift(`<article class="artifact-card"><b>生成项目文件</b><small>将本项目当前生成文件打包下载</small><a href="/api/sessions/${encodeURIComponent(session.id)}/download.zip">下载完整 ZIP ↓</a></article>`);
  $('deliverableContent').innerHTML=content.join('')||'<div class="empty-deliverables">当前阶段还没有成果。<br>完成当前任务后，方案和文件会出现在这里。</div>';
  $('scopeCard').classList.toggle('hidden',!session?.approvals?.development);
}
function renderWorkspace() {
  const has=Boolean(session);
  $('welcome').classList.toggle('hidden',has); $('workspace').classList.toggle('hidden',!has);
  if(!has){$('projectTitle').textContent='新建项目';renderModelSelector();return;}
  if(rightPanelPreferredWidth!==null)applyRightPanelWidth(rightPanelPreferredWidth);
  else updateSplitterAria();
  $('projectTitle').textContent=session.title||'新建项目';
  renderModelSelector();
  const phase=phases.find(p=>p.id===session.phase)||phases[0]; const index=currentIndex();
  $('phaseKicker').textContent=`阶段 ${String(index+1).padStart(2,'0')} / ${phases.length}`;
  $('phaseTitle').textContent=phase.label; $('phaseIntro').textContent=phase.intro;
  $('chatInput').placeholder=phase.id==='brief'?'回答当前问题，也可以补充或纠正已整理的信息…':'补充信息，或针对当前方案提出修改…';
  $('errorBanner').textContent=session.error||''; $('errorBanner').classList.toggle('hidden',!session.error);
  $('runningBanner').classList.toggle('hidden',session.status!=='running');
  $('runningText').textContent=session.status==='running'?`${phase.label}正在进行…`:'Agent 正在思考…';
  renderWorkflow();renderDiscovery();renderControls();renderMessages();renderRight();
  $('chatInput').disabled=session.status==='running'; $('sendMessage').disabled=session.status==='running';
  refreshSessions();
}
async function openSession(id) {
  try { const data=await request(`/api/sessions/${encodeURIComponent(id)}`);session=data.session;activeDeliverable='requirements';renderWorkspace(); }
  catch(e){showError(e.message)}
}
function showError(message) { $('errorBanner').textContent=message;$('errorBanner').classList.remove('hidden'); }
async function createProject(brief) {
  if(!config.configured){openModelSettings();return;}
  if(!draftModel){alert('请先在右上角选择本项目使用的模型。');$('modelBadge').focus();return;}
  try {
    const data=await request('/api/sessions',{method:'POST',body:JSON.stringify({brief,entry_mode:entryMode,model_selection:draftModel})});
    session=data.session;activeDeliverable='requirements';renderWorkspace();
    await startDiscovery();
  } catch(e){showError(e.message)}
}
async function startDiscovery() {
  if(!session)return;
  try {
    session.status='running';session.phase='brief';session.error='';session.discovery=session.discovery||{entry_mode:session.entry_mode||'idea',facts:{},open_questions:[],status:'interviewing'};renderWorkspace();
    const data=await request('/api/discovery/start',{method:'POST',body:JSON.stringify({session_id:session.id})});
    await pollJob(data.job_id);
    if(session?.discovery)session.discovery.started=true;
  } catch(e){session.status='ready';session.error=e.message;renderWorkspace()}
}
async function createPrdDraft() {
  if(!session)return;
  try {const data=await request('/api/discovery/draft',{method:'POST',body:JSON.stringify({session_id:session.id})});session=data.session;activeDeliverable='requirements';renderWorkspace();}
  catch(e){showError(e.message)}
}
async function reopenDiscovery() {
  if(!session)return;
  try {const data=await request('/api/discovery/reopen',{method:'POST',body:JSON.stringify({session_id:session.id})});session=data.session;activeDeliverable='requirements';renderWorkspace();}
  catch(e){showError(e.message)}
}
async function pollJob(jobId) {
  pendingJob=jobId;
  for(let i=0;i<360;i++) {
    await new Promise(r=>setTimeout(r,1000));
    if(pendingJob!==jobId)return;
    try {
      const job=await request(`/api/jobs/${encodeURIComponent(jobId)}`);
      if(job.status==='running')continue;
      const refreshed=await request(`/api/sessions/${encodeURIComponent(session.id)}`);session=refreshed.session;pendingJob=null;
      if(job.status==='failed')session.error=job.error||'模型任务失败。';
      renderWorkspace();return;
    } catch(e){session.error=e.message;pendingJob=null;renderWorkspace();return;}
  }
  pendingJob=null;session.status='ready';session.error='任务等待时间较长，请检查连接后重试。';renderWorkspace();
}
async function runPhase(phase) {
  if(!session)return;
  if(!config.configured){openModelSettings();return;}
  try {
    session.phase=phase;session.status='running';session.error='';renderWorkspace();
    const data=await request('/api/run',{method:'POST',body:JSON.stringify({session_id:session.id,phase})});
    await pollJob(data.job_id);
  } catch(e) { session.status='ready';session.error=e.message;renderWorkspace(); }
}
async function approvePhase(phase) {
  if(!session)return;
  try {
    const payload={session_id:session.id,phase};
    if(phase==='components')payload.selected_components=[...document.querySelectorAll('#componentChoices input:checked')].map(x=>x.value);
    if(phase==='development')payload.generation_scope=$('generationScope')?.value||'fullstack';
    const data=await request('/api/review',{method:'POST',body:JSON.stringify(payload)});session=data.session;
    activeDeliverable=({brief:'components',components:'components',architecture:'architecture',development:session.phase,ui_design:'ui_design'})[phase]||'requirements';
    renderWorkspace();
    if(phase==='development')changePhase(session.phase);
  } catch(e){showError(e.message)}
}
function changePhase(phase) {
  if(!session)return;
  if((phase==='frontend'||phase==='backend')&&!session.approvals?.development){showError('请先确认开发范围与任务拆分。');return;}
  if(phase==='frontend'&&session.generation_scope==='backend'){showError('本次生成范围仅包含后端。');return;}
  if(phase==='backend'&&session.generation_scope==='frontend'){showError('本次生成范围仅包含前端。');return;}
  session.phase=phase;activeDeliverable=phase;renderWorkspace();
}
async function sendChat() {
  const message=$('chatInput').value.trim();if(!message||!session)return;
  if(!config.configured){openModelSettings();return;}
  try {
    session.messages.push({role:'user',content:message});session.status='running';session.error='';$('chatInput').value='';renderWorkspace();
    const data=await request('/api/chat',{method:'POST',body:JSON.stringify({session_id:session.id,message})});
    await pollJob(data.job_id);
  } catch(e){session.status='ready';session.error=e.message;renderWorkspace()}
}
async function continueAnswer() {
  if(!session)return;
  if(!config.configured){openModelSettings();return;}
  try {
    session.status='running';session.error='';renderWorkspace();
    const data=await request('/api/continue',{method:'POST',body:JSON.stringify({session_id:session.id})});
    await pollJob(data.job_id);
  } catch(e){session.status='ready';session.error=e.message;renderWorkspace()}
}
function showSettingsMessage(message, error=true) {$('settingsError').textContent=message;$('settingsError').classList.toggle('hidden',!message);$('settingsError').classList.toggle('success',!error);}
function modelNames() {return [...new Set($('modelNames').value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean))];}
function refreshTestModels() {
  const previous=$('testModel').value, models=modelNames();
  $('testModel').innerHTML=models.length?models.map(m=>`<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join(''):'<option value="">请先填写模型 ID</option>';
  if(models.includes(previous))$('testModel').value=previous;
}
function showProviderList() {
  editingProviderId='';$('apiKey').value='';showSettingsMessage('');
  $('providerListView').classList.remove('hidden');$('providerEditView').classList.add('hidden');
  const providers=config.providers||[];
  $('providerList').innerHTML=providers.length?providers.map(p=>`<article class="provider-row"><div class="provider-info"><strong>${escapeHtml(p.name)}</strong><span>${p.models.map(escapeHtml).join(' · ')}</span></div><div class="provider-actions"><button type="button" class="secondary-button" data-edit-provider="${escapeHtml(p.id)}">编辑</button><button type="button" class="secondary-button" data-delete-provider="${escapeHtml(p.id)}">删除</button></div></article>`).join(''):'<div class="provider-empty">尚未添加模型连接，请先添加。</div>';
  $('providerList').querySelectorAll('[data-edit-provider]').forEach(el=>el.addEventListener('click',()=>editProvider(el.dataset.editProvider)));
  $('providerList').querySelectorAll('[data-delete-provider]').forEach(el=>el.addEventListener('click',()=>deleteProvider(el.dataset.deleteProvider)));
}
function editProvider(id='') {
  editingProviderId=id;
  const p=(config.providers||[]).find(item=>item.id===id);
  $('providerListView').classList.add('hidden');$('providerEditView').classList.remove('hidden');
  $('providerName').value=p?.name||'';$('baseUrl').value=p?.base_url||'';$('modelNames').value=(p?.models||[]).join('\n');$('apiKey').value='';
  $('savedKey').classList.toggle('hidden',!p?.has_key);$('keyField').classList.toggle('hidden',!!p?.has_key);
  refreshTestModels();showSettingsMessage('');
}
async function setupSettings() {
  try {
    await request('/api/models',{method:'POST',body:JSON.stringify({id:editingProviderId,name:$('providerName').value,api_key:$('apiKey').value,base_url:$('baseUrl').value,models:modelNames()})});
    await refreshHealth();showProviderList();
  } catch(e){showSettingsMessage(e.message)}
}
async function deleteProvider(id) {
  if(!confirm('删除此连接？使用该连接的项目需要重新选择模型。'))return;
  try {await request(`/api/models/${encodeURIComponent(id)}/delete`,{method:'POST',body:'{}'});await refreshHealth();showProviderList();}
  catch(e){alert(e.message)}
}
function resetNewProjectForm() {
  feishuImportGeneration++;
  importedFeishuUrls.clear();
  $('briefInput').value='';
  $('briefFile').value='';
  $('fileNotice').textContent='';
  $('fileNotice').classList.add('hidden');
  $('feishuUrl').value='';
  showFeishuNotice('');
  feishuAuthFlowId=null;
  $('feishuAccount').classList.add('hidden');
  $('feishuAuthFlow').classList.add('hidden');
  $('loadFeishuPrd').disabled=false;
  $('startProject').disabled=false;
  setEntryMode('idea');
}
function setEntryMode(mode) {
  entryMode=mode;
  document.querySelectorAll('[data-starter]').forEach(item=>{const selected=item.dataset.mode===mode;item.classList.toggle('selected',selected);item.setAttribute('aria-pressed',String(selected))});
  $('feishuImport').classList.toggle('hidden',mode!=='prd');
  $('briefInput').placeholder=mode==='prd'?'粘贴 PRD 内容、上传文件，或读取飞书文档／Markdown 文件；访谈会优先补齐缺口。':'描述你目前的想法，哪怕只有一句话；访谈会一步步帮你梳理。';
}
function showFeishuNotice(message, error=false) {
  $('feishuNotice').textContent=message;
  $('feishuNotice').classList.toggle('hidden',!message);
  $('feishuNotice').classList.toggle('error',error);
}
async function loadFeishuPrd() {
  const url=$('feishuUrl').value.trim();
  if(!url){showFeishuNotice('请先粘贴飞书文档链接。',true);return false;}
  if(importedFeishuUrls.has(url)&&$('briefInput').value.trim()){showFeishuNotice('文档内容已在上方，可以继续补充后开始梳理。');return true;}
  const generation=++feishuImportGeneration;
  $('loadFeishuPrd').disabled=true;
  $('startProject').disabled=true;
  showFeishuNotice('正在读取飞书文档…');
  try {
    const data=await request('/api/import/feishu',{method:'POST',body:JSON.stringify({url})});
    if(generation!==feishuImportGeneration||$('feishuUrl').value.trim()!==url)return false;
    const existing=$('briefInput').value.trim();
    $('briefInput').value=existing?`${existing}\n\n--- 飞书文档 ---\n${data.content}`:data.content;
    importedFeishuUrls.add(url);
    $('feishuAccount').classList.add('hidden');
    showFeishuNotice('已读取飞书文档。内容已填入上方，您可以补充后开始梳理。');
    return true;
  } catch(e) {
    if(generation===feishuImportGeneration){
      $('feishuAccount').classList.toggle('hidden',!e.authRequired);
      showFeishuNotice(e.authRequired?`${e.message} 请点击“连接飞书账号”。`:e.message,true);
    }
    return false;
  } finally {
    if(generation===feishuImportGeneration){$('loadFeishuPrd').disabled=false;$('startProject').disabled=false;}
  }
}
async function startFeishuAuth() {
  $('connectFeishu').disabled=true;
  showFeishuNotice('正在准备飞书登录…');
  try {
    const data=await request('/api/feishu/auth/start',{method:'POST',body:'{}'});
    feishuAuthFlowId=data.flow_id;
    $('feishuAuthLink').href=data.verification_url;
    $('feishuAuthCode').textContent=data.user_code?`验证码：${data.user_code}`:'';
    $('feishuAuthCode').classList.toggle('hidden',!data.user_code);
    $('feishuAuthQr').src=data.qr_data_url;
    $('feishuAuthFlow').classList.remove('hidden');
    showFeishuNotice('请打开授权页面或扫描二维码，完成后点击“我已完成授权”。');
  } catch(e){showFeishuNotice(e.message,true)}
  finally{$('connectFeishu').disabled=false}
}
async function completeFeishuAuth() {
  if(!feishuAuthFlowId)return;
  $('completeFeishuAuth').disabled=true;
  showFeishuNotice('正在确认飞书授权…');
  try {
    await request('/api/feishu/auth/complete',{method:'POST',body:JSON.stringify({flow_id:feishuAuthFlowId})});
    feishuAuthFlowId=null;
    $('feishuAccount').classList.add('hidden');
    $('feishuAuthFlow').classList.add('hidden');
    showFeishuNotice('飞书账号已连接。');
    if($('feishuUrl').value.trim())await loadFeishuPrd();
  } catch(e){showFeishuNotice(e.message,true)}
  finally{$('completeFeishuAuth').disabled=false}
}
async function startProjectFromWelcome() {
  let brief=$('briefInput').value.trim();
  if(entryMode==='prd'){
    if(/^https:\/\/\S+$/i.test(brief)&&/\/(docx|wiki|file|drive\/file)\//.test(brief)&&!$('feishuUrl').value.trim()){
      $('feishuUrl').value=brief;
      $('briefInput').value='';
      brief='';
    }
    const url=$('feishuUrl').value.trim();
    if(url&&(!importedFeishuUrls.has(url)||!brief)){
      if(!await loadFeishuPrd())return;
      brief=$('briefInput').value.trim();
    }
  }
  createProject(brief);
}
$('newProject').addEventListener('click',()=>{session=null;draftModel=null;renderModelSelector();pendingJob=null;activeDeliverable='requirements';resetNewProjectForm();renderWorkspace();$('briefInput').focus()});
$('startProject').addEventListener('click',startProjectFromWelcome);
$('loadFeishuPrd').addEventListener('click',loadFeishuPrd);
$('connectFeishu').addEventListener('click',startFeishuAuth);
$('completeFeishuAuth').addEventListener('click',completeFeishuAuth);
$('feishuUrl').addEventListener('input',()=>{feishuImportGeneration++;$('loadFeishuPrd').disabled=false;$('startProject').disabled=false;showFeishuNotice('')});
$('feishuUrl').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();loadFeishuPrd()}});
  document.querySelectorAll('[data-starter]').forEach(b=>b.addEventListener('click',()=>{
  feishuImportGeneration++;
  importedFeishuUrls.clear();
  $('feishuUrl').value='';
  showFeishuNotice('');
  feishuAuthFlowId=null;
  $('feishuAccount').classList.add('hidden');
  $('feishuAuthFlow').classList.add('hidden');
  $('loadFeishuPrd').disabled=false;
  $('startProject').disabled=false;
  $('briefFile').value='';
  $('fileNotice').textContent='';
  $('fileNotice').classList.add('hidden');
  setEntryMode(b.dataset.mode||'idea');
  $('briefInput').value='';$('briefInput').focus();
}));
$('briefFile').addEventListener('change',async e=>{
  const file=e.target.files?.[0];if(!file)return;
  if(file.size>2_000_000){$('fileNotice').textContent='文件超过 2 MB，请粘贴内容到需求输入框。';$('fileNotice').classList.remove('hidden');return;}
  try{const content=await file.text();if($('briefFile').files?.[0]!==file)return;setEntryMode('prd');$('briefInput').value=[$('briefInput').value.trim(),`\n\n--- ${file.name} ---\n`,content].filter(Boolean).join('');$('fileNotice').textContent=`已附加：${file.name}`;$('fileNotice').classList.remove('hidden');}
  catch{if($('briefFile').files?.[0]!==file)return;$('fileNotice').textContent='无法读取文件，请复制内容后粘贴。';$('fileNotice').classList.remove('hidden');}
});
$('sendMessage').addEventListener('click',sendChat);
$('chatInput').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat()}});
function openModelSettings(){showProviderList();$('settingsDialog').showModal()}
$('openSettings').addEventListener('click',openModelSettings);
$('modelBadge').addEventListener('change',async e=>{
  const selection=parseModel(e.target.value);
  if(!selection)return;
  if(!session){draftModel=selection;return;}
  try {const data=await request(`/api/sessions/${encodeURIComponent(session.id)}/model`,{method:'POST',body:JSON.stringify({selection})});session=data.session;renderWorkspace();}
  catch(error){showError(error.message);renderModelSelector();}
});
$('newProvider').addEventListener('click',()=>editProvider(''));
$('changeKey').addEventListener('click',()=>{$('savedKey').classList.add('hidden');$('keyField').classList.remove('hidden');$('apiKey').focus()});
$('modelNames').addEventListener('input',refreshTestModels);
$('testProvider').addEventListener('click',async()=>{
  const model=$('testModel').value;
  if(!model){showSettingsMessage('请先填写并选择要测试的模型 ID。');return;}
  $('testProvider').disabled=true;showSettingsMessage('正在测试连接…',false);
  try {await request('/api/models/test',{method:'POST',body:JSON.stringify({id:editingProviderId,api_key:$('apiKey').value,base_url:$('baseUrl').value,model})});showSettingsMessage(`${model} 连接成功。`,false)}catch(e){showSettingsMessage(e.message)}finally{$('testProvider').disabled=false}
});
$('mobileProjectSelect').addEventListener('change',e=>{if(e.target.value)openSession(e.target.value)});
$('cancelSettings').addEventListener('click',showProviderList);
$('settingsForm').addEventListener('submit',e=>{e.preventDefault();setupSettings()});
$('stopWait').addEventListener('click',()=>{$('runningBanner').classList.add('hidden')});
const rightPanelStorageKey='blueprint:rightPanelWidth';
let rightPanelPreferredWidth=null;
try{const saved=Number(localStorage.getItem(rightPanelStorageKey));if(Number.isFinite(saved)&&saved>0)rightPanelPreferredWidth=saved}catch{}
function rightPanelLimits(){
  const grid=$('workGrid');
  const max=Math.max(0,Math.min(window.innerWidth/2,grid.clientWidth-270));
  return {min:Math.min(220,max),max};
}
function updateSplitterAria(){
  const splitter=$('panelSplitter');
  const {min,max}=rightPanelLimits();
  const width=Math.round($('deliverables').getBoundingClientRect().width);
  splitter.setAttribute('aria-valuemin',String(Math.round(min)));
  splitter.setAttribute('aria-valuemax',String(Math.round(max)));
  splitter.setAttribute('aria-valuenow',String(width));
  splitter.setAttribute('aria-valuetext',`右侧工作台宽度 ${width} 像素`);
}
function applyRightPanelWidth(requested,save=false){
  const {min,max}=rightPanelLimits();
  if(!max)return;
  const width=Math.round(Math.max(min,Math.min(max,requested)));
  $('workGrid').style.setProperty('--right-panel-width',`${width}px`);
  updateSplitterAria();
  if(save){rightPanelPreferredWidth=width;try{localStorage.setItem(rightPanelStorageKey,String(width))}catch{}}
}
const panelSplitter=$('panelSplitter');
let activeResizePointer=null;
panelSplitter.addEventListener('pointerdown',event=>{
  if(event.button!==0||window.matchMedia('(max-width: 760px)').matches||$('workGrid').classList.contains('panel-hidden'))return;
  activeResizePointer=event.pointerId;
  panelSplitter.setPointerCapture(event.pointerId);
  panelSplitter.classList.add('dragging');
  document.body.classList.add('resizing-panels');
  event.preventDefault();
});
panelSplitter.addEventListener('pointermove',event=>{
  if(activeResizePointer!==event.pointerId)return;
  const gridRight=$('workGrid').getBoundingClientRect().right;
  applyRightPanelWidth(gridRight-event.clientX-panelSplitter.getBoundingClientRect().width/2);
});
function finishRightPanelResize(event){
  if(activeResizePointer!==event.pointerId)return;
  activeResizePointer=null;
  panelSplitter.classList.remove('dragging');
  document.body.classList.remove('resizing-panels');
  applyRightPanelWidth($('deliverables').getBoundingClientRect().width,true);
}
panelSplitter.addEventListener('pointerup',finishRightPanelResize);
panelSplitter.addEventListener('pointercancel',finishRightPanelResize);
panelSplitter.addEventListener('keydown',event=>{
  if(window.matchMedia('(max-width: 760px)').matches||$('workGrid').classList.contains('panel-hidden'))return;
  const current=$('deliverables').getBoundingClientRect().width;
  const {min,max}=rightPanelLimits();
  const next=({ArrowLeft:current+24,ArrowRight:current-24,Home:min,End:max})[event.key];
  if(next===undefined)return;
  event.preventDefault();
  applyRightPanelWidth(next,true);
});
window.addEventListener('resize',()=>{if(rightPanelPreferredWidth!==null&&session)applyRightPanelWidth(rightPanelPreferredWidth);else if(session)updateSplitterAria()});
$('toggleRight').addEventListener('click',()=>{
  const mobile=window.matchMedia('(max-width: 760px)').matches;
  if(mobile)$('deliverables').classList.toggle('visible');
  else{$('workGrid').classList.toggle('panel-hidden');if(!$('workGrid').classList.contains('panel-hidden'))requestAnimationFrame(updateSplitterAria)}
});
refreshHealth();refreshSessions().then(()=>renderWorkspace());
