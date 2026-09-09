const settings = document.querySelector('#study-settings');
const secondary = document.querySelector('#secondary-settings');
const settingsStatus = document.querySelector('#settings-status');
const uploadStatus = document.querySelector('#upload-status');
const results = document.querySelector('#course-results');
const uploadButton = document.querySelector('#upload-button');
let appliedSettings = null;
let request = null;

// Form choices ship with the page; temporary API downtime must not blank them.
function loadSettingsOptions(data) {
 document.querySelectorAll('.year-select').forEach(select => {
  select.replaceChildren();
  data.years.forEach(year => select.add(new Option(year, year)));
  select.value = select.dataset.default;
 });
 document.querySelectorAll('.program-select').forEach(select => {
  select.replaceChildren();
  data.programs.forEach(program => select.add(new Option(program, program)));
 });
 settings.elements.target.value = '資科';
}
loadSettingsOptions(JSON.parse(document.querySelector('#settings-options').textContent));

settings.addEventListener('change', () => {
 resetAudit();
 const enabled = settings.elements.plan.value !== '單主修';
 secondary.hidden = !enabled;
 secondary.disabled = !enabled;
 appliedSettings = null;
 settingsStatus.textContent = '';
});
settings.addEventListener('submit', event => {
 event.preventDefault();
 resetAudit();
 if (!secondary.disabled && settings.elements.primary.value === settings.elements.target.value) {
  settingsStatus.textContent = '主修與目標系所／組別不能相同。'; return;
 }
 appliedSettings = Object.fromEntries(new FormData(settings));
 const target = secondary.disabled ? '' : `；${appliedSettings.plan}：${appliedSettings.targetYear} ${appliedSettings.target}`;
 settingsStatus.textContent = `已套用：${appliedSettings.admission} 入學，${appliedSettings.handbook} ${appliedSettings.primary}${target}。`;
});

function cell(tag, text) { const node = document.createElement(tag); node.textContent = text ?? ''; return node; }
function clearTranscript() {
 resetAudit(); uploadedFile=null; auditPanel.hidden=true;
 request?.abort(); request = null;
 results.replaceChildren();
 document.querySelector('#transcript-file').value = '';
 uploadButton.disabled = false;
 uploadStatus.textContent = '成績已清除。';
}
document.querySelector('#clear-transcript').addEventListener('click', clearTranscript);
document.querySelector('#upload-form').addEventListener('submit', async event => {
 event.preventDefault();
 const file = document.querySelector('#transcript-file').files[0];
 resetAudit(); uploadedFile=null; auditPanel.hidden=true;
 if (!file || !file.name.toLowerCase().endsWith('.pdf') || file.size > 20 * 1024 * 1024) {
  uploadStatus.textContent = '請選擇 20 MB 以內的 PDF。'; return;
 }
 request?.abort(); const current = new AbortController(); request = current;
 uploadButton.disabled = true; results.replaceChildren();
 uploadStatus.textContent = '正在解析成績單…';
 try {
  const response = await (window.utFetch || fetch)('/api/transcript', {method:'POST', body:file, headers:{'Content-Type':'application/pdf'}, signal:current.signal});
  if (!response.headers.get('content-type')?.includes('application/json')) {
   throw Error('成績解析服務沒有回應，請確認本機服務已啟動後重試。');
  }
  const data = await response.json();
  if (!response.ok) throw Error(data.error || '解析失敗，請重試。');
  uploadStatus.textContent = '';
  if (data.warnings.length) {
   const notes = document.createElement('details'); notes.append(cell('summary','解析核對事項'));
   const list = document.createElement('ul'); data.warnings.forEach(w => list.append(cell('li',w))); notes.append(list); results.append(notes);
  }
  data.groups.forEach(group => {
   results.append(cell('h3',group.label));
   const wrap = document.createElement('div'); wrap.className = 'course-table-scroll'; wrap.tabIndex = 0;
   const table = document.createElement('table'); const head = document.createElement('thead'); const titles = document.createElement('tr');
   ['學期','課程','必選修','學分','成績'].forEach(title=>titles.append(cell('th',title))); head.append(titles); table.append(head);
   const body = document.createElement('tbody'); group.rows.forEach(row=>{
    const tr=document.createElement('tr'); ['term','name','kind','credits','grade'].forEach(key=>tr.append(cell('td',row[key]))); body.append(tr);
   }); table.append(body); wrap.append(table); results.append(wrap);
  });
  uploadedFile=file; auditPanel.hidden=false;
 } catch(error) {
  if (error.name !== 'AbortError') uploadStatus.textContent = error instanceof TypeError
   ? '無法連線到成績解析服務，請確認本機服務已啟動後重試。'
   : error.message || '連線失敗，請重試。';
 } finally { if (request === current) { uploadButton.disabled = false; request = null; } }
});
