<?php
/**
 * Settings — Edit config, prompts, and schema.
 */

$settingsFile = __DIR__ . '/settings.json';
$promptDir = __DIR__ . '/prompts';

$prompts = [
    'extraction' => ['file' => "{$promptDir}/extraction.txt", 'label' => 'Extraction Prompt (Phase 2)'],
    'analysis' => ['file' => "{$promptDir}/analysis.txt", 'label' => 'Analysis Prompt (Phase 4)'],
    'schema' => ['file' => "{$promptDir}/schema.txt", 'label' => 'JSON Schema'],
];

$saved = null;
$tab = $_GET['tab'] ?? 'config';

// Handle saves
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    if (isset($_POST['action']) && $_POST['action'] === 'save_config') {
        $settings = json_decode(file_get_contents($settingsFile), true);
        $settings['model'] = $_POST['model'] ?? $settings['model'];
        $settings['anthropic_api_key'] = $_POST['anthropic_api_key'] ?? $settings['anthropic_api_key'];
        $settings['openai_api_key'] = $_POST['openai_api_key'] ?? ($settings['openai_api_key'] ?? '');
        $settings['browserbase_api_key'] = $_POST['browserbase_api_key'] ?? $settings['browserbase_api_key'];
        $settings['browserbase_project_id'] = $_POST['browserbase_project_id'] ?? $settings['browserbase_project_id'];
        $settings['sec_user_agent'] = $_POST['sec_user_agent'] ?? $settings['sec_user_agent'];
        $settings['max_page_chars'] = (int) ($_POST['max_page_chars'] ?? $settings['max_page_chars']);
        $settings['max_entity_names'] = (int) ($_POST['max_entity_names'] ?? $settings['max_entity_names']);
        $settings['max_ciks'] = (int) ($_POST['max_ciks'] ?? $settings['max_ciks']);
        $settings['max_ownership_levels'] = (int) ($_POST['max_ownership_levels'] ?? $settings['max_ownership_levels']);
        $blockedRaw = $_POST['blocked_entity_names'] ?? '';
        $settings['blocked_entity_names'] = array_values(array_filter(array_map('trim', explode("\n", $blockedRaw))));
        file_put_contents($settingsFile, json_encode($settings, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES));
        $saved = 'config';
        $tab = 'config';
    } elseif (isset($_POST['prompt_key'])) {
        $key = $_POST['prompt_key'];
        if (isset($prompts[$key]) && isset($_POST['content'])) {
            file_put_contents($prompts[$key]['file'], $_POST['content']);
            $saved = $key;
            $tab = 'prompts';
        }
    }
}

$settings = json_decode(file_get_contents($settingsFile), true);
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Entity Lookup — Settings</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f7; color: #333; min-height: 100vh; }

  .header { background: #1a1a2e; color: #fff; padding: 24px 40px; display: flex; justify-content: space-between; align-items: center; }
  .header h1 { font-size: 22px; font-weight: 600; }
  .header h1 a { text-decoration: none; color: inherit; }
  .header p { font-size: 13px; color: #8a8aaf; margin-top: 4px; }
  .header nav a { color: #8a8aaf; text-decoration: none; font-size: 13px; margin-left: 16px; }
  .header nav a:hover { color: #fff; }

  .content { max-width: 1000px; margin: 32px auto; padding: 0 40px; }

  .tabs { display: flex; gap: 0; margin-bottom: 24px; border-bottom: 2px solid #e0e0e0; }
  .tab { padding: 10px 24px; font-size: 14px; font-weight: 600; color: #666; cursor: pointer; text-decoration: none; border-bottom: 2px solid transparent; margin-bottom: -2px; }
  .tab:hover { color: #333; }
  .tab.active { color: #4a90d9; border-bottom-color: #4a90d9; }

  .tab-content { display: none; }
  .tab-content.active { display: block; }

  .form-group { margin-bottom: 16px; }
  .form-label { display: block; font-size: 13px; font-weight: 600; color: #555; margin-bottom: 4px; }
  .form-input { width: 100%; padding: 10px 12px; font-size: 14px; border: 2px solid #e0e0e0; border-radius: 6px; outline: none; font-family: 'SF Mono', 'Fira Code', monospace; }
  .form-input:focus { border-color: #4a90d9; }
  .form-input-short { width: 120px; }
  .form-hint { font-size: 11px; color: #888; margin-top: 2px; }

  .form-row { display: flex; gap: 16px; }
  .form-row .form-group { flex: 1; }

  .prompt-textarea { width: 100%; min-height: 220px; padding: 14px; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px; line-height: 1.6; border: 2px solid #e0e0e0; border-radius: 8px; resize: vertical; outline: none; }
  .prompt-textarea:focus { border-color: #4a90d9; }

  .prompt-section { margin-bottom: 28px; }
  .prompt-label { font-size: 14px; font-weight: 600; color: #1a1a2e; margin-bottom: 8px; }

  .save-btn { padding: 10px 24px; background: #4a90d9; color: #fff; border: none; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; margin-top: 8px; }
  .save-btn:hover { background: #3a7bc8; }

  .saved-msg { display: inline-block; margin-left: 12px; color: #16a34a; font-size: 13px; font-weight: 600; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1><a href="/">Entity Lookup</a></h1>
    <p>Settings</p>
  </div>
  <nav>
    <a href="/">Lookup</a>
    <a href="/settings.php">Settings</a>
  </nav>
</div>

<div class="content">

<div class="tabs">
  <a href="?tab=config" class="tab <?= $tab === 'config' ? 'active' : '' ?>">Configuration</a>
  <a href="?tab=prompts" class="tab <?= $tab === 'prompts' ? 'active' : '' ?>">Prompts</a>
</div>

<!-- Config Tab -->
<div class="tab-content <?= $tab === 'config' ? 'active' : '' ?>">
  <form method="post">
    <input type="hidden" name="action" value="save_config">

    <div class="form-group">
      <label class="form-label">Model</label>
      <?php
        $models = [
            'Claude' => [
                'claude-sonnet-4-6' => 'Claude Sonnet 4.6',
                'claude-sonnet-4-5-20250514' => 'Claude Sonnet 4.5',
                'claude-haiku-4-5-20251001' => 'Claude Haiku 4.5',
                'claude-opus-4-6' => 'Claude Opus 4.6',
            ],
            'OpenAI' => [
                'gpt-4o' => 'GPT-4o',
                'gpt-4o-mini' => 'GPT-4o Mini',
                'o3' => 'o3',
                'o4-mini' => 'o4-mini',
            ],
        ];
        $currentModel = $settings['model'] ?? '';
      ?>
      <select name="model" class="form-input" style="width:auto;min-width:280px;">
        <?php foreach ($models as $group => $groupModels): ?>
          <optgroup label="<?= $group ?>">
            <?php foreach ($groupModels as $id => $label): ?>
              <option value="<?= $id ?>" <?= $id === $currentModel ? 'selected' : '' ?>><?= $label ?></option>
            <?php endforeach; ?>
          </optgroup>
        <?php endforeach; ?>
      </select>
      <?php if (!isset(array_merge(...array_values($models))[$currentModel])): ?>
        <div class="form-hint" style="color:#e67e22;">Current model "<?= htmlspecialchars($currentModel) ?>" is not in the dropdown — it will be preserved unless you change it.</div>
      <?php endif; ?>
    </div>

    <div class="form-group">
      <label class="form-label">Anthropic API Key</label>
      <input type="password" name="anthropic_api_key" class="form-input" value="<?= htmlspecialchars($settings['anthropic_api_key']) ?>">
    </div>

    <div class="form-group">
      <label class="form-label">OpenAI API Key</label>
      <input type="password" name="openai_api_key" class="form-input" value="<?= htmlspecialchars($settings['openai_api_key'] ?? '') ?>">
    </div>

    <div class="form-group">
      <label class="form-label">Browserbase API Key</label>
      <input type="password" name="browserbase_api_key" class="form-input" value="<?= htmlspecialchars($settings['browserbase_api_key']) ?>">
    </div>

    <div class="form-group">
      <label class="form-label">Browserbase Project ID</label>
      <input type="text" name="browserbase_project_id" class="form-input" value="<?= htmlspecialchars($settings['browserbase_project_id']) ?>">
    </div>

    <div class="form-group">
      <label class="form-label">SEC User Agent</label>
      <input type="text" name="sec_user_agent" class="form-input" value="<?= htmlspecialchars($settings['sec_user_agent']) ?>">
      <div class="form-hint">SEC requires a valid User-Agent with contact email</div>
    </div>

    <div class="form-row">
      <div class="form-group">
        <label class="form-label">Max Page Chars</label>
        <input type="number" name="max_page_chars" class="form-input form-input-short" value="<?= $settings['max_page_chars'] ?>">
      </div>
      <div class="form-group">
        <label class="form-label">Max Entity Names</label>
        <input type="number" name="max_entity_names" class="form-input form-input-short" value="<?= $settings['max_entity_names'] ?>">
      </div>
      <div class="form-group">
        <label class="form-label">Max CIKs</label>
        <input type="number" name="max_ciks" class="form-input form-input-short" value="<?= $settings['max_ciks'] ?>">
      </div>
      <div class="form-group">
        <label class="form-label">Max Ownership Levels</label>
        <input type="number" name="max_ownership_levels" class="form-input form-input-short" value="<?= $settings['max_ownership_levels'] ?>">
      </div>
    </div>

    <div class="form-group">
      <label class="form-label">Blocked Entity Names</label>
      <textarea name="blocked_entity_names" class="prompt-textarea" style="min-height:120px"><?= htmlspecialchars(implode("\n", $settings['blocked_entity_names'] ?? [])) ?></textarea>
      <div class="form-hint">One per line. Names matched case-insensitively. Use for WHOIS registrars and other false positives.</div>
    </div>

    <button type="submit" class="save-btn">Save Configuration</button>
    <?php if ($saved === 'config'): ?>
      <span class="saved-msg">Saved!</span>
    <?php endif; ?>
  </form>
</div>

<!-- Prompts Tab -->
<div class="tab-content <?= $tab === 'prompts' ? 'active' : '' ?>">
  <?php foreach ($prompts as $key => $info): ?>
    <div class="prompt-section">
      <form method="post">
        <input type="hidden" name="prompt_key" value="<?= $key ?>">
        <div class="prompt-label"><?= htmlspecialchars($info['label']) ?></div>
        <textarea name="content" class="prompt-textarea"><?= htmlspecialchars(file_get_contents($info['file'])) ?></textarea>
        <button type="submit" class="save-btn">Save</button>
        <?php if ($saved === $key): ?>
          <span class="saved-msg">Saved!</span>
        <?php endif; ?>
      </form>
    </div>
  <?php endforeach; ?>
</div>

</div>
</body>
</html>
