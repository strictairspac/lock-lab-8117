import {table} from '../components/common/table.js';
import {badge} from '../components/common/badge.js';
import {MIGRATION_STATES,LEDGERS} from '../constants/enums.js';
import {esc,date} from '../utils/html.js';
import {session} from '../hooks/session.js';

function migrationPanel(m){
  if(!m)return '';
  const admin=session.user?.role==='admin';
  const canStart=admin&&(m.status==='idle'||m.status==='returned');
  const canBatch=admin&&m.status==='migrating';
  const canSwitch=admin&&['migrating','ready'].includes(m.status);
  const canReturn=admin&&m.active_ledger==='receipt_ledger';
  const partitions=(m.partitions||[]).map(p=>`<li><code>${esc(p.name)}</code> ${esc(p.bound)}</li>`).join('');
  return `<section class="migration" id="migration-panel">
    <div class="section-top"><h2>回执账本迁移</h2><span>${esc(MIGRATION_STATES[m.status]||m.status)} · 当前读取 ${esc(LEDGERS[m.active_ledger]||m.active_ledger)}</span></div>
    <div class="migration-grid">
      <div><span class="eyebrow">对账 / RECONCILE</span>
        <p>旧表 ${m.legacy_count} 行 · 分表 ${m.ledger_count} 行 · 缺 ${m.missing_count} 行 · 多 ${m.extra_count} 行 · 无月份 ${m.unplaced_count} 行 · 游标 ${m.cursor}</p>
        <p class="migration-verdict ${m.balanced?'ok':'warn'}">${m.balanced?'两侧逐行一致，可以切换':'两侧尚未核齐，切换会被拒绝'}</p>
        ${m.error?`<p class="error">${esc(m.error)}</p>`:''}
        ${m.started_at?`<small>发起 ${date(m.started_at)}${m.activated_at?' · 切换 '+date(m.activated_at):''}${m.returned_at?' · 退回 '+date(m.returned_at):''}</small>`:''}
      </div>
      <div><span class="eyebrow">按月分区 / PARTITIONS</span>
        ${partitions?`<ul class="partitions">${partitions}</ul>`:'<p>尚无分区；回填或新回执写入时按演练发起月份自动建立。</p>'}
      </div>
    </div>
    <div class="migration-actions">
      <button id="mig-start" ${canStart?'':'disabled'}>发起迁移</button>
      <button id="mig-batch" ${canBatch?'':'disabled'}>回填一批</button>
      <button id="mig-run" ${canBatch?'':'disabled'}>回填至核齐</button>
      <button class="primary" id="mig-switch" ${canSwitch?'':'disabled'}>核齐后切换</button>
      <button id="mig-return" ${canReturn?'':'disabled'}>退回旧表</button>
    </div>
    ${admin?'':'<p class="help">迁移与切换仅管理员可操作；观察员与操作员可查看状态。</p>'}
    <p class="help">旧回执按演练发起月份归档；切换期间新回执照常提交，跨月提交也归原月。退回旧表后，新账本启用期间提交的回执仍可在旧表查到。</p>
  </section>`;
}

export function evidencePage(trial,migration){
  const receiptBlock=trial?`<div class="section-top"><h2>回执来源</h2><span>${esc(LEDGERS[trial.ledger]||trial.ledger||'')} · 本场 ${trial.receipts?.length||0} 条</span></div>${table(['回执','经办','数量','归入月份','来源'],(trial.receipts||[]).map(r=>[`#${r.id}`,esc(r.actor),`${r.quantity} 份`,esc(r.period||trial.period||'—'),esc(r.origin==='live'?'实时写入':(r.origin==='backfill'?'历史回填':''))]))}`:'';
  const body=!trial?'<div class="empty">尚无冲突证据。请到「并发执行」发起演练，完成后在此复核事务回滚结果。</div>':(()=>{const r=trial.result;return `<div class="evidence-top"><div><span class="eyebrow">EVIDENCE / #${trial.id}</span><h2>${esc(trial.name)} ${badge(trial.status)}</h2><p>${date(trial.created_at)} · PostgreSQL 真实双连接</p></div><button id="export" data-id="${trial.id}">导出 JSON ↓</button></div>${trial.error?`<div class="error">${esc(trial.error)}。请检查配置后重新发起演练。</div>`:''}${r?`<div class="verdict"><div class="verdict-icon">${r.invariant_passed?'✓':'!'}</div><div><span class="eyebrow">CONSERVATION CHECK</span><h2>${r.invariant_passed?'回滚完整，数量守恒':'不变量检查未通过'}</h2><p>${r.initial} 份 = 剩余 ${r.remaining} 份 + 已接收 ${r.received} 份 · 提交回执 ${r.receipt_count} 条 · 归入 ${esc(r.receipt_period||trial.period||'—')}</p></div><div class="equation">${r.remaining}<span> + </span>${r.received}<span> = </span>${r.initial}</div></div><div class="section-top"><h2>数据库事务轨迹</h2><span>证据来自实际连接与错误码</span></div>${table(['连接','Backend PID','事务事件','等待 / SQLSTATE'],r.traces.map(t=>[`<strong>事务 ${esc(t.actor)}</strong>`,t.pid,esc(t.event),t.sqlstate?`${t.wait_ms} ms / ${esc(t.sqlstate)}`:(t.event==='row_lock_acquired'?'已获取行锁':'提交成功')]))}${receiptBlock}<div class="evidence-notes"><section><h3>判定口径</h3><p>${esc(r.formula)}</p><p>B 事务先插入的回执，已随 lock_timeout 事务回滚。A 持锁等待由线程事件协调，未伪造等待结果。</p></section><section><h3>复现摘要</h3><p>${esc(r.mode)}</p><code>${esc(r.checksum)}</code></section></div>`:'<div class="notice">任务正在后台执行，页面每 1.5 秒刷新。可以保留此页或稍后从运行列表返回。</div>'}`;})();
  return `${migrationPanel(migration)}${body}`;
}
