import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { dispatch } from './worker.mjs';
const env = { GITHUB_TOKEN: 'test-only', VERIFY_TOKEN: 'verify-only' };
for (const [cron, session, workflow] of [
  ['30 2 * * 1-5', 'morning', 'account-daily-telegram.yml'],
  ['0 6 * * 1-5', 'closing', 'account-intraday-telegram.yml'],
]) {
  test(`${cron} dispatches ${session}`, async () => {
    let calls = 0;
    await dispatch(cron, env, false, async (url, options) => {
      calls++;
      assert.ok(url.endsWith(`/${workflow}/dispatches`));
      assert.deepEqual(JSON.parse(options.body), { ref: 'main', inputs: { session, dry_run: 'false' } });
      assert.equal(options.headers.Authorization, 'Bearer test-only');
      return new Response(null, { status: 204 });
    });
    assert.equal(calls, 1);
  });
}
test('unknown and absent cron never dispatch', async () => {
  for (const cron of [undefined, '', '0 0 * * *', '0 6 * * *', '__proto__', 'constructor']) {
    assert.deepEqual(await dispatch(cron, {}, false, () => assert.fail()), { skipped: true });
  }
});
test('GitHub non-2xx and network failure reject with observable failure', async () => {
  const original = console.error;
  const logs = [];
  console.error = message => logs.push(message);
  try {
    for (const status of [401, 403, 422, 429, 500]) {
      await assert.rejects(dispatch('0 6 * * 1-5', env, false,
        async () => new Response(null, { status })), /GitHub dispatch HTTP/);
    }
    await assert.rejects(dispatch('0 6 * * 1-5', env, false,
      async () => { throw new Error('timeout'); }), /timeout/);
    assert.equal(logs.length, 6);
    assert.ok(logs.every(x => x.includes('dispatch_failed') && !x.includes(env.GITHUB_TOKEN)));
  } finally { console.error = original; }
});
test('scheduled handler propagates failure', async () => {
  await assert.rejects(worker.scheduled({ cron: '0 6 * * 1-5' }, {}), /Missing GITHUB_TOKEN/);
});
test('endpoint rejects unauthorized, invalid session and non-target requests', async () => {
  for (const [path, method, token, status] of [
    ['/verify?session=morning','POST','wrong',401],
    ['/verify?session=nope','POST','verify-only',400],
    ['/','POST','verify-only',404],
    ['/verify?session=closing','GET','verify-only',404],
  ]) {
    assert.equal((await worker.fetch(new Request(`https://example.com${path}`,
      { method, headers: { Authorization: `Bearer ${token}` } }),env)).status,status);
  }
});
test('verification endpoint can only dispatch dry-run', async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async (_url, options) => {
    assert.deepEqual(JSON.parse(options.body).inputs, { session: 'closing', dry_run: 'true' });
    return new Response(null, { status: 204 });
  };
  try {
    const response = await worker.fetch(new Request('https://example.com/verify?session=closing',
      { method: 'POST', headers: { Authorization: 'Bearer verify-only' } }),env);
    assert.equal(response.status,200);
  } finally { globalThis.fetch = original; }
});
