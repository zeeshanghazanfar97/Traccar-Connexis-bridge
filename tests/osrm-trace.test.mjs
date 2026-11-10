import assert from 'node:assert/strict';
import test from 'node:test';
import { matchTrace, traceBatches } from '../osrm/traccar/web/matchTrace.mjs';

const fix = (second, overrides = {}) => ({
  deviceId: 1, valid: true, longitude: 67, latitude: 24,
  fixTime: new Date(second * 1000).toISOString(), ...overrides,
});

test('long traces overlap once per 100-fix OSRM request, including the final leg', () => {
  const positions = Array.from({ length: 200 }, (_, i) => fix(i));
  const { batches, partial } = traceBatches(positions);
  assert.equal(partial, false);
  assert.deepEqual(batches.map(({ points }) =>
    [points.length, points[0].timestamp, points.at(-1).timestamp]),
  [[100, 0, 99], [100, 99, 198], [2, 198, 199]]);
});

test('invalid fixes, device changes, reversed time and long gaps break the trace', () => {
  const positions = [
    fix(1), fix(61), fix(122), fix(123), fix(124, { valid: false }), fix(125), fix(126),
    fix(120), fix(121), fix(122, { deviceId: 2 }), fix(123, { deviceId: 2 }),
  ];
  const { batches, partial } = traceBatches(positions);
  assert.equal(partial, true);
  assert.deepEqual(batches.map(({ deviceId, points }) => [deviceId, ...points.map(p => p.timestamp)]),
    [[1, 1, 61], [1, 122, 123], [1, 125, 126], [1, 120, 121], [2, 122, 123]]);
});

test('subsecond fixes coalesce and malformed or isolated fixes are never sent', async () => {
  const positions = [fix(10), fix(10.5), fix(11), fix(12, { longitude: null }), fix(13),
    fix(14, { latitude: 91 }), fix(15, { fixTime: null })];
  const { batches, partial } = traceBatches(positions);
  assert.equal(partial, true);
  assert.deepEqual(batches[0].points, [
    { longitude: 67, latitude: 24, timestamp: 10 },
    { longitude: 67, latitude: 24, timestamp: 11 },
  ]);
  assert.equal(batches.length, 1);
  assert.deepEqual(await matchTrace([fix(10), fix(1000)]), { lines: [], status: 'empty' });
});

test('OSRM submatches and no-match batches remain separate', async (t) => {
  const first = [[67, 24], [67.01, 24.01]];
  const second = [[67.1, 24.1], [67.2, 24.2]];
  const replies = [{ lines: [first, second], partial: true }, { lines: [], partial: true }];
  const signal = new AbortController().signal;
  const fetch = t.mock.method(globalThis, 'fetch', async (url, init) => {
    assert.equal(url, 'api/osrm/match?deviceId=1');
    assert.equal(init.signal, signal);
    assert.equal(init.method, 'POST');
    assert.equal(JSON.parse(init.body).length, 2);
    return Response.json(replies.shift());
  });
  const result = await matchTrace([fix(1), fix(2), fix(100), fix(101)], signal);
  assert.deepEqual(result, { lines: [first, second], status: 'partial' });
  assert.equal(fetch.mock.callCount(), 2);
});

test('service failures do not synthesize a matched path', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response(null, { status: 502 }));
  await assert.rejects(matchTrace([fix(1), fix(2)]), /unavailable/);
});
