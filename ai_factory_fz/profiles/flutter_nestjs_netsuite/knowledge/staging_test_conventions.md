Staging and test conventions (each of these cost a release fix round when missed):
- Rate limiting: the API's limiter is configurable (THROTTLE_TTL, THROTTLE_LIMIT). infra/staging.env sets a high
  test-only limit (e.g. THROTTLE_LIMIT=100000) so smoke and device tests never get 429. Production keeps the
  default. infra/ files belong to the Deployment engineer: add every new staging variable there in the same
  work item that introduces it.
- Smoke tests start by emptying the cart through the API (the mock keeps carts between runs), then sign in with
  SMOKE_USER_EMAIL / SMOKE_USER_PASSWORD (defaults: the mock's buyer@example.com / demo-password-1).
- Smoke tests check error codes and statuses exactly as the contract states them (e.g. 409 TOTALS_CHANGED in the
  error field), and the backend returns exactly those codes.
- Device (integration_test) tests find widgets by Key or semantics label, never by exact visible text: wording
  changes break text finders. Give every tappable element and every value a test checks a stable Key.
- Device tests run against staging through the emulator (API_BASE_URL=http://10.0.2.2:<staging port>/api/v1,
  passed with --dart-define). Use timeouts of at least 30 seconds per step; the first build is slow.
- Test fixtures and expectations use the mock's documented users and items (see staging_testing.md); do not
  invent ids.
- The smoke suite file is server/smoke/smoke.test.ts, run by npm run test:smoke; its vitest config must include
  that path (a wrong include pattern makes the run fail with "No test files found").
