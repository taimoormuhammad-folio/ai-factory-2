Backend conventions (NestJS):
- One Nest module per bounded area: auth, users, catalog, cart, orders, payments, health.
- Controllers stay thin; logic lives in services; Prisma access goes through PrismaService.
- DTOs use class-validator; responses match the OpenAPI schemas exactly. The OpenAPI document is the contract; the code follows it.
- Global prefix /api/v1. JWT bearer auth with access tokens (15 min) and refresh tokens.
- Errors return { statusCode, error, message } with correct HTTP codes.
- List endpoints paginate with page and pageSize, returning items and total.
- Config via environment variables validated at startup (@nestjs/config). No secrets in code.
- Tests: Jest unit tests per service; Supertest e2e tests per controller.
- GET /api/v1/health returns 200 when the database is reachable.

## Found by the first canary releases (keep these from recurring)
- Register ONE global exception filter (`APP_FILTER`) that turns every error, including unhandled ones, into
  `{ statusCode, error, message }`. Nest's default body for an unhandled exception has no `error` field, which breaks the contract.
- Staging must start with its data: run the migrations and the seed in the container entrypoint (or the compose command),
  so a fresh stack serves the seeded records without anyone running a command by hand.
- Use the fixed ids from the design when seeding (upsert by id), so tests and smoke checks can rely on them.
