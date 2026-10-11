Backend conventions (NestJS backend-for-frontend in front of NetSuite SuiteCommerce):
- The API has no database. NetSuite is the source of truth for customers, items, prices, cart and orders;
  never copy them into local storage. No Prisma, no PostgreSQL, no Redis.
- One Nest module per area: auth, catalog, cart, checkout, health; plus a suitecommerce module with the only
  HTTP client that talks to SuiteCommerce (all URLs, headers, cookie handling and error mapping live there).
- Stateless sessions: on login, put the SuiteCommerce session cookies and the customer context (internalid,
  subsidiary, currency id, role id, payment terms id) in an encrypted token (AES-256-GCM with node:crypto,
  key from the SESSION_ENCRYPTION_KEY environment variable, 32 bytes base64). Return it as a bearer token;
  decrypt it on every request. The token expires after 20 minutes of inactivity, like the NetSuite session.
  Never store or log the user's password.
- An expired NetSuite session does not fail loudly (the cart just reads as empty), so cart and checkout
  calls first confirm the session (Profile isLoggedIn). When it is gone, or a call returns a not-logged-in
  error, return 401 with error SESSION_EXPIRED so the app asks the user to sign in again.
- Map SuiteCommerce errors (they can come with HTTP 200, see the API reference) to the shared error schema
  { statusCode, error, message } with the right HTTP status; 502 when SuiteCommerce is unreachable or
  returns something unexpected, with a generic message (details only in the server log).
- Responses are small and shaped for the app's screens: never pass SuiteCommerce payloads through as is.
  Money as integer minor units (cents) plus a currency code and a formatted string from the server.
- Strip or sanitise HTML descriptions on the server.
- Global prefix /api/v1; DTOs with class-validator; the OpenAPI document is the contract. Lists paginate
  with page and pageSize and return items and total.
- Config via @nestjs/config, validated at startup: SUITECOMMERCE_BASE_URL, SUITECOMMERCE_COMPANY_ID,
  SUITECOMMERCE_SITE_ID, SUITECOMMERCE_CATALOG_FILTER, SESSION_ENCRYPTION_KEY, CHECKOUT_SUBMIT_ENABLED,
  SUITECOMMERCE_TIMEOUT_MS (default 15000). No secrets or hosts in code.
- Outbound calls have a timeout and no automatic retry for POST/PUT/DELETE (they change the cart).
- Tests: Jest unit tests per service with the SuiteCommerce client mocked (nock); Supertest e2e tests per
  controller. Test fixtures can follow the shapes in infra/suitecommerce-mock/fixtures. Tests never call
  the real site.
- GET /api/v1/health returns 200 when the API is up (it must not depend on SuiteCommerce being reachable).
