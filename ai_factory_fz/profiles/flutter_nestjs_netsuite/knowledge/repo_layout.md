Repository layout (fixed by the build tooling; it creates these folders and runs the coding agents in them):
- server/: the NestJS backend-for-frontend. Never apps/api, backend/ or services/.
- app/: the Flutter app. Never apps/mobile, mobile/ or client/.
- app/packages/api_client/: the dart-dio client generated from the contract. Never edited by hand.
- docs/api-contract.yaml: the API contract, the single source of truth. Copies live at server/openapi.yaml and
  app/openapi.yaml and must stay identical. Never packages/api-contract/ or contracts/.
- infra/: staging files (docker-compose.staging.yml, staging.env, README.md).
- infra/suitecommerce-mock/: a working Node mock with fixtures, README and Dockerfile, provided ready-made by
  the profile. Extend it (new fixtures, scenarios, endpoints); never rewrite it in another framework.
- .github/workflows/: CI.

Do not add other top-level folders (no apps/, packages/, libs/, contracts/). Designs and work items name
these exact paths; an architecture that uses other folders is sent back (guardrail A3).
