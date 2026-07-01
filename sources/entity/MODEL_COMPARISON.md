# Model Comparison — How to Run

## Quick Start

Run a URL through all three models and regenerate the comparison page:

```bash
cd /Users/craiganderson/Dropbox/dev/entity-lookup

# Run Sonnet
ANTHROPIC_API_KEY=$(python3 -c "import json; print(json.load(open('php/settings.json'))['anthropic_api_key'])") \
python3 -c "
import asyncio, json, os
from agent_v3b import run_agent_v3b
async def run():
    url = 'https://www.example.com/'  # <-- change this
    result = await run_agent_v3b(url, os.environ['ANTHROPIC_API_KEY'], model='claude-sonnet-4-20250514', provider='anthropic')
    r = result['report']; r['_meta'] = result['meta']
    domain = url.replace('https://','').replace('http://','').replace('www.','').strip('/')
    os.makedirs('reports/sonnet_v3b', exist_ok=True)
    json.dump(r, open(f'reports/sonnet_v3b/{domain}.json','w'), indent=2, ensure_ascii=False)
    print(f'Sonnet: {(r.get(\"recommended_entity\") or {}).get(\"legal_entity_name\",\"None\")} | \${result[\"meta\"][\"actual_cost_usd\"]:.4f}')
asyncio.run(run())
"

# Run Haiku
ANTHROPIC_API_KEY=$(python3 -c "import json; print(json.load(open('php/settings.json'))['anthropic_api_key'])") \
python3 -c "
import asyncio, json, os
from agent_v3b import run_agent_v3b
async def run():
    url = 'https://www.example.com/'  # <-- change this
    result = await run_agent_v3b(url, os.environ['ANTHROPIC_API_KEY'], model='claude-haiku-4-5-20251001', provider='anthropic')
    r = result['report']; r['_meta'] = result['meta']
    domain = url.replace('https://','').replace('http://','').replace('www.','').strip('/')
    os.makedirs('reports/haiku_v3b', exist_ok=True)
    json.dump(r, open(f'reports/haiku_v3b/{domain}.json','w'), indent=2, ensure_ascii=False)
    print(f'Haiku: {(r.get(\"recommended_entity\") or {}).get(\"legal_entity_name\",\"None\")} | \${result[\"meta\"][\"actual_cost_usd\"]:.4f}')
asyncio.run(run())
"

# Run GPT-4o
OPENAI_API_KEY="sk-REDACTED" \
python3 -c "
import asyncio, json, os
from agent_v3b import run_agent_v3b
async def run():
    url = 'https://www.example.com/'  # <-- change this
    result = await run_agent_v3b(url, os.environ['OPENAI_API_KEY'], model='gpt-4o', provider='openai')
    r = result['report']; r['_meta'] = result['meta']
    domain = url.replace('https://','').replace('http://','').replace('www.','').strip('/')
    os.makedirs('reports/openai_v3b', exist_ok=True)
    json.dump(r, open(f'reports/openai_v3b/{domain}.json','w'), indent=2, ensure_ascii=False)
    print(f'GPT-4o: {(r.get(\"recommended_entity\") or {}).get(\"legal_entity_name\",\"None\")} | \${result[\"meta\"][\"actual_cost_usd\"]:.4f}')
asyncio.run(run())
"

# Regenerate comparison HTML
python3 compare.py
```

## Regenerate Comparison Page Only

If reports already exist and you just want to rebuild the HTML:

```bash
python3 compare.py              # generates compare.html and opens it
python3 compare.py --no-open    # generates without opening
python3 compare.py --serve      # serves at http://localhost:8001 with live rebuild
```

## Report Directories

- `reports/sonnet_v3b/` — Claude Sonnet 4 results
- `reports/haiku_v3b/` — Claude Haiku 4.5 results
- `reports/openai_v3b/` — GPT-4o results

Each report is a JSON file named `{domain}.json` with `_meta` embedded for cost/timing data.

## API Keys

- **Anthropic**: stored in `php/settings.json` → `anthropic_api_key`
- **OpenAI**: `sk-REDACTED`

## Models

| Model | Model ID | Provider |
|-------|----------|----------|
| Sonnet 4 | `claude-sonnet-4-20250514` | anthropic |
| Haiku 4.5 | `claude-haiku-4-5-20251001` | anthropic |
| GPT-4o | `gpt-4o` | openai |
