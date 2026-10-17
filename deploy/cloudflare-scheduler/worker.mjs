const TARGETS = Object.freeze({
  '30 2 * * 1-5': ['morning', 'account-daily-telegram.yml'],
  '0 6 * * 1-5': ['closing', 'account-intraday-telegram.yml'],
});

export async function dispatch(cron, env, dryRun = false, fetcher = fetch) {
  const target = Object.hasOwn(TARGETS, cron) ? TARGETS[cron] : undefined;
  if (!target) return { skipped: true };
  if (!env.GITHUB_TOKEN) throw new Error('Missing GITHUB_TOKEN Worker Secret');
  const [session, workflow] = target;
  try {
    const response = await fetcher(
      `https://api.github.com/repos/keyuc606-dev/etf-quant-assistant/actions/workflows/${workflow}/dispatches`,
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: 'application/vnd.github+json',
          'User-Agent': 'etf-cloudflare-scheduler',
          'X-GitHub-Api-Version': '2022-11-28',
        },
        body: JSON.stringify({ ref: 'main', inputs: { session, dry_run: String(dryRun) } }),
        signal: AbortSignal.timeout(15000),
        redirect: 'error',
      },
    );
    if (!response.ok) throw new Error(`GitHub dispatch HTTP ${response.status}`);
    console.log(JSON.stringify({ event: 'dispatch_accepted', session, dryRun, status: response.status }));
    return { accepted: true, session, dryRun };
  } catch (error) {
    // Never log credentials, response bodies, or request headers. No automatic retry:
    // a timeout may mean GitHub already accepted the dispatch.
    console.error(JSON.stringify({ event: 'dispatch_failed', session }));
    throw error;
  }
}

export default {
  async scheduled(event, env) {
    // Unknown cron strings are inert. Final calendar/window/idempotency gates run in Python.
    return dispatch(event.cron, env);
  },
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method !== 'POST' || url.pathname !== '/verify') return new Response('Not found', { status: 404 });
    if (!env.VERIFY_TOKEN || request.headers.get('Authorization') !== `Bearer ${env.VERIFY_TOKEN}`) {
      return new Response('Unauthorized', { status: 401 });
    }
    const session = url.searchParams.get('session');
    const cron = Object.keys(TARGETS).find(key => TARGETS[key][0] === session);
    if (!cron) return new Response('Invalid session', { status: 400 });
    try {
      return Response.json(await dispatch(cron, env, true));
    } catch {
      return new Response('Dispatch failed; inspect Worker logs', { status: 502 });
    }
  },
};
