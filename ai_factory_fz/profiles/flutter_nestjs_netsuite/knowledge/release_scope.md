Release scope of this pipeline (plan only what it can build and verify):
- Release = staging with docker compose (the API plus the SuiteCommerce mock) and an Android debug APK tested on
  an emulator. Production deployment is a separate command run after the release gate.
- Out of scope (manual steps outside the pipeline, never work items): store distribution (TestFlight, Google Play
  upload, store listings), code signing and signed AAB/IPA builds, CodeQL or other paid scanners.
- Crash reporting and analytics sit behind an app interface with a no-op implementation in version 1: no Firebase
  SDK and no google-services.json or GoogleService-Info.plist (they need company Firebase projects; add a real
  provider later behind the same interface).
- Pilot distribution is a manual step: document it in infra/README.md, do not automate it.
