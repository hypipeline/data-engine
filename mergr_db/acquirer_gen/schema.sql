-- Acquirer & Precedent Generator — run log for cost tracking + model comparison.
CREATE SCHEMA IF NOT EXISTS acquirer_gen;

CREATE TABLE IF NOT EXISTS acquirer_gen.runs (
  id            bigserial PRIMARY KEY,
  created_at    timestamptz DEFAULT now(),
  target        text,
  provider      text,
  model         text,
  settings      jsonb,
  prompt_hash   text,
  input_tokens  int,
  output_tokens int,
  web_searches  int,
  cost_usd      numeric,
  latency_ms    int,
  n_acquirers   int,
  n_deals       int,
  n_in_on       int,     -- acquirers matched to an ON buyer
  n_in_mergr    int,     -- acquirers in Mergr (firm/company) but not ON
  n_net_new     int,     -- acquirers not found in ON or Mergr (candidates to verify)
  parse_ok      boolean,
  error         text,
  result        jsonb
);
CREATE INDEX IF NOT EXISTS runs_created_idx ON acquirer_gen.runs (created_at DESC);
CREATE INDEX IF NOT EXISTS runs_model_idx   ON acquirer_gen.runs (model);
