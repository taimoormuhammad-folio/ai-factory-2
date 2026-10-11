import assert from 'node:assert/strict';
import test from 'node:test';
import { FZ_GATES, fzGateFromPending, gateForStage } from '../src/gates.js';

test('fzGateFromPending maps prd to G1 spec review', () => {
  const g = fzGateFromPending({
    gate: 'prd',
    gate_id: 'G1',
    summary: '3 stories',
    documents: ['docs/prd.md'],
  });
  assert.equal(g.index, 1);
  assert.equal(g.kind, 'spec');
  assert.equal(g.gate, 'prd');
  assert.equal(g.gate_id, 'G1');
});

test('ui gate maps to design stage', () => {
  const g = fzGateFromPending({ gate: 'ui', documents: ['docs/ui/index.html'] });
  assert.equal(g.index, FZ_GATES.ui.index);
  assert.equal(g.kind, 'design');
});

test('merge gate needs risk note', () => {
  const g = fzGateFromPending({ gate: 'merge', needs_risk_note: true });
  assert.equal(g.needsRiskNote, true);
});

test('gateForStage falls back to react-base demo gates', () => {
  const info = gateForStage(7, null);
  assert.equal(info.kind, 'deploy');
});
