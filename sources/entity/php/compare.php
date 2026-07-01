<?php
/**
 * Model Comparison — Run the same URL through multiple models in parallel.
 */
$settings = json_decode(file_get_contents(__DIR__ . '/settings.json'), true);
$defaultModel = $settings['model'] ?? '';

$models = [
    'Claude' => [
        'claude-sonnet-4-6' => 'Sonnet 4.6',
        'claude-sonnet-4-5-20250514' => 'Sonnet 4.5',
        'claude-haiku-4-5-20251001' => 'Haiku 4.5',
        'claude-opus-4-6' => 'Opus 4.6',
    ],
    'OpenAI' => [
        'gpt-4o' => 'GPT-4o',
        'gpt-4o-mini' => 'GPT-4o Mini',
        'o3' => 'o3',
        'o4-mini' => 'o4-mini',
    ],
];
$allModels = array_merge(...array_values($models));
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Entity Lookup — Model Comparison</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f7; color: #333; min-height: 100vh; }

  .header { background: #1a1a2e; color: #fff; padding: 24px 40px; display: flex; justify-content: space-between; align-items: center; }
  .header h1 { font-size: 22px; font-weight: 600; }
  .header h1 a { text-decoration: none; color: inherit; }
  .header p { font-size: 13px; color: #8a8aaf; margin-top: 4px; }
  .header nav a { color: #8a8aaf; text-decoration: none; font-size: 13px; margin-left: 16px; }
  .header nav a:hover { color: #fff; }

  .content { max-width: 1400px; margin: 32px auto; padding: 0 40px; }

  .compare-form { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 24px; }
  .compare-input { flex: 1; min-width: 300px; padding: 12px 16px; font-size: 15px; border: 2px solid #e0e0e0; border-radius: 8px; outline: none; }
  .compare-input:focus { border-color: #4a90d9; }
  .compare-btn { padding: 12px 28px; background: #4a90d9; color: #fff; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; }
  .compare-btn:hover { background: #3a7bc8; }
  .compare-btn:disabled { background: #999; cursor: not-allowed; }

  .model-checkboxes { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }
  .model-group { background: #fff; border-radius: 8px; padding: 12px 16px; border: 2px solid #e0e0e0; }
  .model-group-label { font-size: 11px; font-weight: 700; text-transform: uppercase; color: #888; margin-bottom: 8px; letter-spacing: 0.5px; }
  .model-check { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; cursor: pointer; font-size: 13px; }
  .model-check input { cursor: pointer; }
  .model-check .default-tag { font-size: 10px; color: #4a90d9; font-weight: 600; }

  .results-grid { display: grid; gap: 24px; }
  .results-grid.cols-2 { grid-template-columns: 1fr 1fr; }
  .results-grid.cols-3 { grid-template-columns: 1fr 1fr 1fr; }
  .results-grid.cols-4 { grid-template-columns: 1fr 1fr 1fr 1fr; }

  .result-col { background: #fff; border-radius: 12px; border: 2px solid #e0e0e0; overflow: hidden; }
  .result-col.conf-high { border-color: #27ae60; }
  .result-col.conf-medium { border-color: #f39c12; }
  .result-col.conf-low { border-color: #e67e22; }
  .result-col.conf-insufficient { border-color: #e74c3c; }

  .result-model-header { padding: 12px 16px; background: #f8f8fa; border-bottom: 1px solid #e0e0e0; display: flex; justify-content: space-between; align-items: center; }
  .result-model-name { font-size: 14px; font-weight: 700; color: #1a1a2e; }
  .result-model-meta { font-size: 11px; color: #888; }

  .result-status { padding: 40px 16px; text-align: center; color: #888; font-size: 13px; }
  .result-status .spinner { display: inline-block; width: 20px; height: 20px; border: 3px solid #e0e0e0; border-top-color: #4a90d9; border-radius: 50%; animation: spin 0.8s linear infinite; margin-bottom: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .result-body { padding: 16px; font-size: 13px; }
  .result-entity { font-size: 16px; font-weight: 700; color: #1a1a2e; margin-bottom: 8px; }
  .result-row { display: flex; gap: 8px; margin-bottom: 4px; }
  .result-label { color: #888; min-width: 90px; flex-shrink: 0; }
  .result-value { color: #333; word-break: break-word; }

  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; text-transform: uppercase; }
  .badge-high { background: #e8f5e9; color: #27ae60; }
  .badge-medium { background: #fff3e0; color: #f39c12; }
  .badge-low { background: #fbe9e7; color: #e67e22; }
  .badge-insufficient { background: #fce4ec; color: #e74c3c; }

  .section-header { font-size: 12px; font-weight: 700; color: #555; margin: 12px 0 6px; text-transform: uppercase; letter-spacing: 0.3px; border-bottom: 1px solid #f0f0f0; padding-bottom: 4px; }
  .evidence-item { font-size: 12px; color: #555; margin-bottom: 3px; line-height: 1.4; }
  .evidence-link { color: #4a90d9; text-decoration: none; font-size: 11px; }
  .evidence-link:hover { text-decoration: underline; }

  .diff-highlight { background: #fff9c4; padding: 0 2px; }

  @media (max-width: 900px) {
    .results-grid.cols-2, .results-grid.cols-3, .results-grid.cols-4 { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1><a href="/">Entity Lookup</a></h1>
    <p>Model Comparison</p>
  </div>
  <nav>
    <a href="/">Lookup</a>
    <a href="/compare.php" style="color:#fff;">Compare</a>
    <a href="/settings.php">Settings</a>
  </nav>
</div>

<div class="content">

<form class="compare-form" id="compare-form">
  <input type="url" id="compare-url" class="compare-input" placeholder="https://www.example.com/" required>
  <button type="submit" class="compare-btn" id="compare-btn">Compare</button>
</form>

<div class="model-checkboxes" id="model-checkboxes">
<?php foreach ($models as $group => $groupModels): ?>
  <div class="model-group">
    <div class="model-group-label"><?= $group ?></div>
    <?php foreach ($groupModels as $id => $label): ?>
      <label class="model-check">
        <input type="checkbox" name="models" value="<?= $id ?>">
        <?= $label ?>
        <?php if ($id === $defaultModel): ?>
          <span class="default-tag">(default)</span>
        <?php endif; ?>
      </label>
    <?php endforeach; ?>
  </div>
<?php endforeach; ?>
</div>

<div id="results-container"></div>

</div>

<script>
const modelLabels = <?= json_encode($allModels) ?>;

document.getElementById('compare-form').addEventListener('submit', function(e) {
  e.preventDefault();

  const url = document.getElementById('compare-url').value.trim();
  if (!url) return;

  const checked = [...document.querySelectorAll('input[name="models"]:checked')].map(cb => cb.value);
  if (checked.length < 2) {
    alert('Select at least 2 models to compare.');
    return;
  }

  const btn = document.getElementById('compare-btn');
  btn.disabled = true;
  btn.textContent = 'Running...';

  const container = document.getElementById('results-container');
  const colClass = checked.length <= 2 ? 'cols-2' : checked.length === 3 ? 'cols-3' : 'cols-4';
  container.innerHTML = `<div class="results-grid ${colClass}" id="results-grid"></div>`;
  const grid = document.getElementById('results-grid');

  // Create columns for each model
  checked.forEach(model => {
    const col = document.createElement('div');
    col.className = 'result-col';
    col.id = `col-${model}`;
    col.innerHTML = `
      <div class="result-model-header">
        <span class="result-model-name">${modelLabels[model] || model}</span>
        <span class="result-model-meta" id="meta-${model}"></span>
      </div>
      <div class="result-status" id="status-${model}">
        <div class="spinner"></div>
        <div>Starting...</div>
      </div>
      <div class="result-body" id="body-${model}" style="display:none;"></div>
    `;
    grid.appendChild(col);
  });

  // Fire all lookups in parallel
  let completed = 0;
  const results = {};

  checked.forEach(model => {
    pollModel(url, model, () => {
      completed++;
      if (completed === checked.length) {
        btn.disabled = false;
        btn.textContent = 'Compare';
        highlightDiffs(checked, results);
      }
    }, results, true);
  });
});

function pollModel(url, model, onDone, results, isFirst) {
  // First call uses refresh=1 to start a new lookup; subsequent polls check cache
  const refresh = isFirst ? '&refresh=1' : '';
  const apiUrl = `/?format=json${refresh}&url=${encodeURIComponent(url)}&model=${encodeURIComponent(model)}`;
  const statusEl = document.getElementById(`status-${model}`);
  const bodyEl = document.getElementById(`body-${model}`);
  const metaEl = document.getElementById(`meta-${model}`);
  const colEl = document.getElementById(`col-${model}`);

  fetch(apiUrl).then(r => r.json()).then(data => {
    if (data.status === 'processing') {
      statusEl.innerHTML = '<div class="spinner"></div><div>Processing...</div>';
      setTimeout(() => pollModel(url, model, onDone, results, false), 3000);
    } else if (data.status === 'complete') {
      results[model] = data;
      renderResult(model, data, statusEl, bodyEl, metaEl, colEl);
      onDone();
    } else {
      statusEl.innerHTML = `<div>Error: ${data.error || 'Unknown'}</div>`;
      onDone();
    }
  }).catch(err => {
    statusEl.innerHTML = `<div>Fetch error: ${err.message}</div>`;
    onDone();
  });
}

function renderResult(model, data, statusEl, bodyEl, metaEl, colEl) {
  statusEl.style.display = 'none';
  bodyEl.style.display = 'block';

  const report = data.report || {};
  const meta = data.meta || {};
  const entity = report.recommended_entity || null;
  const confidence = report.confidence || 'insufficient';

  colEl.className = `result-col conf-${confidence}`;

  // Meta line
  metaEl.innerHTML = `$${(meta.cost_usd || 0).toFixed(2)} · ${(meta.total_time_s || 0).toFixed(1)}s · ${(meta.input_tokens || 0).toLocaleString()} in / ${(meta.output_tokens || 0).toLocaleString()} out`;

  let html = '';

  // Entity name + confidence
  const entityName = entity ? entity.legal_entity_name : 'No match found';
  html += `<div class="result-entity" data-field="entity">${esc(entityName)} <span class="badge badge-${confidence}">${confidence}</span></div>`;

  // Note
  if (report.note) {
    html += `<div class="evidence-item" style="margin-bottom:12px;" data-field="note">${esc(report.note)}</div>`;
  }

  if (entity) {
    html += `<div class="result-row"><span class="result-label">Jurisdiction</span><span class="result-value" data-field="jurisdiction">${esc(entity.jurisdiction_description || entity.jurisdiction || '—')}</span></div>`;
    html += `<div class="result-row"><span class="result-label">Registry ID</span><span class="result-value" data-field="registry_id">${esc(entity.registry_id || '—')}</span></div>`;
    html += `<div class="result-row"><span class="result-label">Address</span><span class="result-value" data-field="address">${esc(entity.address || '—')}</span></div>`;
    html += `<div class="result-row"><span class="result-label">Source</span><span class="result-value">${entity.source_url ? `<a href="${esc(entity.source_url)}" target="_blank" class="evidence-link">${esc(entity.source || '—')}</a>` : esc(entity.source || '—')}</span></div>`;
  }

  // Registry validation
  const rv = report.registry_validation;
  if (rv) {
    const rvClass = rv.status === 'verified' ? 'badge-high' : 'badge-insufficient';
    const rvLabel = rv.status === 'verified' ? 'Registry Verified' : rv.status === 'name_match_bad_status' ? 'Inactive' : rv.status === 'name_mismatch' ? 'Mismatch' : 'Not Found';
    html += `<div class="result-row"><span class="result-label">Validation</span><span class="result-value"><span class="badge ${rvClass}" data-field="validation">${rvLabel}</span></span></div>`;
  }

  // Substance
  if (report.substance_score !== undefined) {
    html += `<div class="result-row"><span class="result-label">Substance</span><span class="result-value" data-field="substance">${report.substance_score}/10 (${report.substance_band || '—'})</span></div>`;
  }

  // Evidence forward
  if (report.evidence_forward && report.evidence_forward.length > 0) {
    html += `<div class="section-header">Forward Evidence (${report.evidence_forward.length})</div>`;
    report.evidence_forward.forEach(ev => {
      html += `<div class="evidence-item">${esc(ev.step || '')} — ${esc(ev.description || '')}</div>`;
    });
  }

  // Evidence reverse
  if (report.evidence_reverse && report.evidence_reverse.length > 0) {
    html += `<div class="section-header">Reverse Validation (${report.evidence_reverse.length})</div>`;
    report.evidence_reverse.forEach(ev => {
      html += `<div class="evidence-item"><span class="badge badge-${ev.strength || 'none'}">${ev.strength || '—'}</span> ${esc(ev.step || '')} — ${esc(ev.description || '')}</div>`;
    });
  }

  // Key people
  if (report.key_people && report.key_people.length > 0) {
    html += `<div class="section-header">Key People (${report.key_people.length})</div>`;
    report.key_people.forEach(p => {
      html += `<div class="evidence-item">${esc(p.name || '')} — ${esc(p.role || '')}</div>`;
    });
  }

  // Affiliates
  if (report.contractable_affiliates && report.contractable_affiliates.length > 0) {
    html += `<div class="section-header">Affiliates (${report.contractable_affiliates.length})</div>`;
    report.contractable_affiliates.forEach(ca => {
      html += `<div class="evidence-item"><strong>${esc(ca.legal_entity_name || '')}</strong> — ${esc(ca.registry_id || '')} ${esc(ca.jurisdiction_country || '')}</div>`;
    });
  }

  bodyEl.innerHTML = html;
}

function highlightDiffs(models, results) {
  // Compare key fields across models and highlight differences
  const fields = ['entity', 'jurisdiction', 'registry_id', 'address', 'substance', 'validation'];
  fields.forEach(field => {
    const values = {};
    models.forEach(m => {
      const el = document.querySelector(`#col-${m} [data-field="${field}"]`);
      if (el) values[m] = el.textContent.trim();
    });
    const uniqueValues = [...new Set(Object.values(values))];
    if (uniqueValues.length > 1) {
      // Values differ — highlight them
      models.forEach(m => {
        const el = document.querySelector(`#col-${m} [data-field="${field}"]`);
        if (el) el.classList.add('diff-highlight');
      });
    }
  });
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
</script>
</body>
</html>
