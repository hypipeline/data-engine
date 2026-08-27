# mergr_db tests

Front-end tests for the Data Engine templates. Plain Node, no dependencies:

```bash
node tests/highlight.test.js       # Buyer Match query-term highlighting
```

Each test pulls the function bodies straight out of `templates/*.html` at run time, so it always
exercises the code the page actually ships rather than a copy that can drift.

## Fixtures

`fixtures/highlight_corpus.json` — real buyer descriptions + sector keywords and real mandate
teaser texts, used as a differential corpus (old implementation vs new, ~22k pairs). Regenerate
from the local Postgres if it ever needs refreshing:

```sql
select json_agg(t) from (select description, sector_keywords from buyer_match.buyers
                         where description is not null and length(description) > 80
                         order by id limit 250) t;
select json_agg(t) from (select code, project_name, summary, points_paragraph_top, points::text
                         from buyer_match.mandates where summary is not null order by id limit 400) t;
```

`trigger_query` in that file is a redacted stand-in for a mandate's GPT document summary — keep it
redacted. Real doc-cache summaries name the target, its revenue and EBITDA on live pre-marketing
deals and must not be committed.
