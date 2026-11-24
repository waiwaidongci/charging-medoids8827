import {api, download} from '../api/client.js';
import {empty, errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {actions} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc, formData, time} from '../utils/html.js';

export async function overview() {
  const profile = state.profile;
  const history = await api('/audit');
  return `<section class="summary"><h2>${esc(profile.organization)}</h2><p>${esc(profile.context)}</p><ol>${profile.scenario.map(step => `<li>${esc(step)}</li>`).join('')}</ol><a class="button" href="#1">核对数据</a></section><section><h2>最近操作</h2>${table(history.map(row => ({时间:time(row.at),账号:row.actor,操作:actions[row.action] ?? row.action,记录:row.subject})))}</section>`;
}

export async function source() {
  const profile = state.profile;
  const [revisions, meters] = await Promise.all([api('/datasets'), api('/meters')]);
  const active = revisions.find(row => row.id === state.datasetId) || revisions[0];
  const detail = active ? await api(`/datasets/${active.id}`) : null;
  const importer = state.account.role === 'analyst' ? `<form id="upload" class="inline-form"><label>导入数据文件<input name="file" type="file" accept=".csv,text/csv" required></label><button type="submit">保存数据版本</button>${errorSlot}</form>` : '';
  const meterForm = state.account.role === 'analyst' ? `<form id="meter" class="inline-form"><label>场站<input name="depot" maxlength="40" required></label><label>批次日期<input name="batch" type="date" required></label><label>表计电量（kWh）<input name="energy_kwh" type="number" step="any" min="0" required></label><label>允许误差（kWh）<input name="tolerance_kwh" type="number" step="any" min="0" required></label><label>出具时间<input name="issued_at" type="datetime-local"></label><button type="submit">登记表计记录</button>${errorSlot}</form>` : '';
  const meterRows = table(meters.map(row => ({场站:row.depot, 批次:row.batch, '表计电量(kWh)':row.energy_kwh, '允许误差(kWh)':row.tolerance_kwh, 出具时间:time(row.issued_at), 登记时间:time(row.recorded_at), 状态:row.current ? '当前' : '历史'})));
  return `<section><p>${esc(profile.input)}</p><p class="hint">CSV 表头顺序可调整，列名需匹配样例。</p>${importer}<button type="button" id="example" class="secondary">下载样例 CSV</button><p id="download-error" role="alert" class="error"></p></section>${detail ? `<section><label>数据版本<select id="revision">${revisions.map(row => `<option value="${row.id}" ${row.id === active.id ? 'selected' : ''}>${esc(row.name)} · #${row.id}</option>`).join('')}</select></label><p class="hint">${detail.count} 行 · 页面预览前 ${detail.records.length} 行 · SHA-256 ${esc(detail.checksum.slice(0,12))}</p>${table(detail.records,profile.columns)}</section>` : empty('请导入项目样例或业务 CSV。')}<section><h2>表计电量记录</h2><p class="hint">重发冲突按同批同场站的表计电量对账，以出具时间最新的记录为准；更正后基于旧表计的待发布版本自动失效，已送达班报原样保留。</p>${meterForm}${meterRows}</section>`;
}

export function bindSource(root, refresh) {
  bindForm(root, '#upload', async form => {
    const file = form.elements.file.files[0];
    const imported = await api('/datasets', {name:file.name,csv:await file.text()});
    state.datasetId = imported.id;
    state.status = imported.created ? `已保存 ${imported.rows} 行。` : '数据与已有版本一致，已选中该版本。';
  }, refresh);
  bindForm(root, '#meter', async form => {
    const data = formData(form);
    data.energy_kwh = Number(data.energy_kwh);
    data.tolerance_kwh = Number(data.tolerance_kwh);
    data.issued_at = data.issued_at ? new Date(data.issued_at).toISOString() : null;
    const saved = await api('/meters', data);
    state.status = saved.duplicated ? '相同表计记录已存在，未重复登记。' : `表计记录已登记${saved.invalidated ? `，${saved.invalidated} 个待发布版本因更正失效` : ''}。`;
  }, refresh);
  root.querySelector('#revision')?.addEventListener('change', event => {
    state.datasetId = Number(event.target.value);
    refresh();
  });
  root.querySelector('#example')?.addEventListener('click', async () => {
    try {await download('/example.csv', state.profile.filename);}
    catch (error) {root.querySelector('#download-error').textContent = error.message;}
  });
}
