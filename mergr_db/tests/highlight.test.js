#!/usr/bin/env node
/**
 * Buyer Match — query-term highlighting tests.
 *
 * Runs the REAL functions shipped in templates/buyer_match.html (pulled out of the file at
 * run time, so the test can never drift from the page) over a corpus of REAL Data Engine data:
 * buyer descriptions and mandate texts (tests/fixtures/highlight_corpus.json).
 *
 * Regression under test: highlight() used to loop word-by-word over the same string, so a query
 * containing the word "mark" — the reported mandate's GPT doc summary ended "...led by CEO Mark
 * <surname>" — matched the <mark> tags it had just inserted and rewrote them into literal
 * "<mark>...</mark>" text all over the results.
 *
 *   node mergr_db/tests/highlight.test.js
 */
const fs = require('fs');
const path = require('path');

const TPL = path.join(__dirname, '..', 'templates', 'buyer_match.html');
const src = fs.readFileSync(TPL, 'utf8');

// ---- pull the shipped implementation out of the template -----------------------------
function extractFn(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i === -1) throw new Error('cannot find function ' + name + ' in ' + TPL);
  const start = src.indexOf('{', i);
  let depth = 0;
  for (let j = start; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(i, j + 1);
  }
  throw new Error('unbalanced braces extracting ' + name);
}
function extractLine(prefix) {
  const line = src.split('\n').find(l => l.trimStart().startsWith(prefix));
  if (!line) throw new Error('cannot find line starting with ' + prefix);
  return line;
}
const shipped = [extractLine('var STOP_WORDS ='), extractFn('esc'), extractFn('getQueryWords'),
                 extractFn('highlight'), extractFn('firstSentence')].join('\n');
const { esc, getQueryWords, highlight, firstSentence } =
  new Function(shipped + '\nreturn {esc, getQueryWords, highlight, firstSentence};')();

// the pre-fix implementation, kept verbatim for the differential test below
function oldHighlight(escaped, words) {
  if (!words.length) return escaped;
  words.forEach(function (w) {
    var pat = new RegExp('\\b(' + w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')\\b', 'gi');
    escaped = escaped.replace(pat, '<mark>$1</mark>');
  });
  return escaped;
}

// ---- tiny harness --------------------------------------------------------------------
let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  ok   ' + name); }
  else { fail++; console.log('  FAIL ' + name + (extra ? '\n       ' + extra : '')); }
}
function eq(name, got, want) { ok(name, got === want, 'got:  ' + got + '\n       want: ' + want); }

const fixtures = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'highlight_corpus.json'), 'utf8'));
const stripMarks = s => s.replace(/<\/?mark>/g, '');
const ENTITY_WORDS = ['amp', 'quot', 'lt', 'gt'];

// The exact text the page builds for the reported mandate (mandate_text + doc summary),
// see buyer_match/mandate.py full_text() and buyer_match.html's fullText assembly.
const NEURO_DOC = fixtures.trigger_query.text;
const NEURO_QUERY = [
  'Summary: IT services and consulting provider',
  '',
  '• IT services and consulting provider with an established, decades-long track record',
  '• Owns a proprietary software platform supporting long-standing international client relationships',
  '• Actively pursuing acquisitions as part of a broader growth strategy',
  '• Offers a broad service range spanning software development, systems integration and channel partner solutions',
  '', 'Document summaries:', '', 'Brainsquare Opportunity Summary:', NEURO_DOC, ''
].join('\n');

const BUYSSE = 'Founded in 2008 by Frank Buysse, Buysse & Partners is an independent investment firm headquartered in Antwerp, Belgium.';

// =====================================================================================
console.log('\n1. the reported case — a mandate whose doc summary names a CEO called Mark');
{
  const words = getQueryWords(NEURO_QUERY);
  ok("corpus really contains the trigger: 'mark' is a query word", words.includes('mark'));
  ok('the doc summary is where it comes from (a CEO called Mark)', /\bMark\b/.test(NEURO_DOC), 'doc summary tail: ' + NEURO_DOC.slice(-70));
  ok('no mandate teaser text in the corpus carries it — doc summaries are the route',
     fixtures.mandates.every(m => !getQueryWords(m.text).includes('mark')));

  const out = highlight(firstSentence(BUYSSE), words);
  ok('no literal tag text leaks into the page', !/<<mark|<\/<mark|&lt;mark/.test(out), out);
  ok('genuine query words are still highlighted, exactly once each',
     out.includes('<mark>Founded</mark>') && (out.match(/<mark>/g) || []).length === 1, out);
  eq('content is untouched once the <mark> wrappers are stripped', stripMarks(out), firstSentence(BUYSSE));

  const before = oldHighlight(firstSentence(BUYSSE), words);
  ok('old implementation did corrupt this case (test is meaningful)', /<<mark/.test(before), before);

  // the same query over every buyer description + keyword pill in the corpus
  let bad = 0;
  fixtures.buyers.forEach(b => {
    if (/<<mark|<\/<mark/.test(highlight(esc(b.description), words))) bad++;
    (b.sector_keywords || '').split(',').map(s => s.trim()).filter(Boolean)
      .forEach(k => { if (/<<mark|<\/<mark/.test(highlight(esc(k), words))) bad++; });
  });
  eq('0 corrupted rows across ' + fixtures.buyers.length + ' real buyers (descriptions + keyword pills)', bad, 0);
}

console.log('\n2. normal highlighting still works (nothing else broken)');
{
  const w = getQueryWords('software systems integration for logistics');
  eq('wraps each match once, preserving original case',
     highlight(esc('Software and systems integration for Logistics clients'), w),
     '<mark>Software</mark> and <mark>systems</mark> <mark>integration</mark> for <mark>Logistics</mark> clients');
  eq('whole words only — "partner" does not match "partners"',
     highlight(esc('partners'), ['partner']), 'partners');
  eq('no query words -> unchanged', highlight(esc('anything at all'), []), 'anything at all');
  eq('no match -> unchanged', highlight(esc('nothing here'), ['fintech']), 'nothing here');
  eq('duplicate words do not double-wrap',
     highlight(esc('fintech'), ['fintech', 'fintech']), '<mark>fintech</mark>');
  eq('multi-word keyword pills (the keyword-browse path) work as phrases',
     highlight(esc('medical devices and diagnostics'), ['medical devices']),
     '<mark>medical devices</mark> and diagnostics');
  eq('regex metacharacters in a keyword are escaped, not interpreted as syntax',
     highlight(esc('match a.b not axb'), ['a.b']), 'match <mark>a.b</mark> not axb');
  eq('same escaping behaviour as before the fix (parity on odd keywords)',
     highlight(esc('r&d (europe) spend'), ['(europe)']), oldHighlight(esc('r&d (europe) spend'), ['(europe)']));
  eq('escaped markup in the source data stays escaped (no XSS route)',
     highlight(esc('<script>alert(1)</script> fintech'), ['fintech']),
     '&lt;script&gt;alert(1)&lt;/script&gt; <mark>fintech</mark>');
}

console.log('\n3. HTML entities from esc() survive query words that look like entity names');
{
  ENTITY_WORDS.forEach(w => {
    const out = highlight(esc('Smith & Jones said "yes" to <deals>'), [w]);
    ok('query word "' + w + '" leaves esc() entities intact', stripMarks(out) === esc('Smith & Jones said "yes" to <deals>') && !/&<mark>|<mark>amp<\/mark>|<mark>quot<\/mark>|<mark>lt<\/mark>|<mark>gt<\/mark>/.test(out), out);
  });
  eq('a genuine word "amp" in the text is still highlighted',
     highlight(esc('amp & watt'), ['amp']), '<mark>amp</mark> &amp; watt');
}

console.log('\n4. property + differential sweep over the real corpus');
{
  const queries = fixtures.mandates.map(m => ({ label: m.code, text: m.text }))
    .concat([{ label: 'trigger_query', text: NEURO_QUERY }]);
  const texts = fixtures.buyers.map(b => b.description);
  let pairs = 0, invariantBad = 0, diff = 0, diffUnexpected = 0, oldCorrupt = 0, newCorrupt = 0, marked = 0;
  const offenders = [];
  queries.forEach(q => {
    const words = getQueryWords(q.text);
    if (!words.length) return;
    const risky = words.includes('mark') || words.some(w => ENTITY_WORDS.includes(w));
    texts.forEach(t => {
      pairs++;
      const e = esc(t);
      const now = highlight(e, words);
      const before = oldHighlight(e, words);
      if (stripMarks(now) !== e) invariantBad++;                       // must only ADD wrappers
      if (/<<mark|<\/<mark|&<mark>/.test(now)) newCorrupt++;
      if (/<<mark|<\/<mark|&<mark>/.test(before)) oldCorrupt++;
      if (now.includes('<mark>')) marked++;
      if (now !== before) {
        diff++;
        if (!risky) { diffUnexpected++; if (offenders.length < 3) offenders.push(q.label + ' :: ' + before.slice(0, 160)); }
      }
    });
  });
  console.log('     ' + queries.length + ' real queries x ' + texts.length + ' real buyer descriptions = ' + pairs.toLocaleString() + ' pairs');
  ok('every output is the input plus <mark> wrappers only (' + pairs.toLocaleString() + ' pairs)', invariantBad === 0, invariantBad + ' violations');
  ok('highlighting actually fires (' + marked.toLocaleString() + ' pairs got at least one <mark>)', marked > pairs * 0.5);
  ok('new implementation never produces broken markup', newCorrupt === 0, newCorrupt + ' corrupted');
  ok('old implementation did (' + oldCorrupt.toLocaleString() + ' pairs) — the bug was real and widespread', oldCorrupt > 0);
  ok('behaviour is byte-identical to before except where the old code was broken',
     diffUnexpected === 0, diffUnexpected + ' unexpected differences\n       ' + offenders.join('\n       '));
  console.log('     ' + diff.toLocaleString() + ' pairs changed, all of them previously-corrupt output');
}

console.log('\n' + (fail ? 'FAILED ' + fail + ' / ' + (pass + fail) : 'PASSED ' + pass + ' / ' + pass) + ' assertions\n');
process.exit(fail ? 1 : 0);
