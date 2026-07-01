<?php
/**
 * Prompt Editor — Edit extraction, analysis, and schema prompts.
 */

$promptDir = __DIR__ . '/prompts';
$files = [
    'extraction' => ['file' => "{$promptDir}/extraction.txt", 'label' => 'Extraction Prompt (Phase 2)'],
    'analysis' => ['file' => "{$promptDir}/analysis.txt", 'label' => 'Analysis Prompt (Phase 4)'],
    'schema' => ['file' => "{$promptDir}/schema.txt", 'label' => 'JSON Schema'],
];

$saved = null;

// Handle save
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['prompt_key'])) {
    $key = $_POST['prompt_key'];
    if (isset($files[$key]) && isset($_POST['content'])) {
        file_put_contents($files[$key]['file'], $_POST['content']);
        $saved = $key;
    }
}

?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Entity Lookup — Prompts</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f7; color: #333; min-height: 100vh; }

  .header { background: #1a1a2e; color: #fff; padding: 24px 40px; display: flex; justify-content: space-between; align-items: center; }
  .header h1 { font-size: 22px; font-weight: 600; }
  .header h1 a { text-decoration: none; color: inherit; }
  .header p { font-size: 13px; color: #8a8aaf; margin-top: 4px; }
  .header nav a { color: #8a8aaf; text-decoration: none; font-size: 13px; margin-left: 16px; }
  .header nav a:hover { color: #fff; }
  .header nav a.active { color: #3fb950; font-weight: 600; }

  .content { max-width: 1000px; margin: 32px auto; padding: 0 40px; }

  .prompt-section { margin-bottom: 32px; }
  .prompt-label { font-size: 14px; font-weight: 600; color: #1a1a2e; margin-bottom: 8px; }
  .prompt-textarea { width: 100%; min-height: 250px; padding: 16px; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px; line-height: 1.6; border: 2px solid #e0e0e0; border-radius: 8px; resize: vertical; outline: none; }
  .prompt-textarea:focus { border-color: #4a90d9; }

  .save-btn { padding: 10px 24px; background: #4a90d9; color: #fff; border: none; border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; margin-top: 8px; }
  .save-btn:hover { background: #3a7bc8; }

  .saved-msg { display: inline-block; margin-left: 12px; color: #16a34a; font-size: 13px; font-weight: 600; }
</style>
</head>
<body>

<div class="header">
  <div>
    <h1><a href="/">Entity Lookup</a></h1>
    <p>Prompt Editor</p>
  </div>
  <nav>
    <a href="/">Lookup</a>
    <a href="/prompts.php" class="active">Prompts</a>
  </nav>
</div>

<div class="content">
  <?php foreach ($files as $key => $info): ?>
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

</body>
</html>
