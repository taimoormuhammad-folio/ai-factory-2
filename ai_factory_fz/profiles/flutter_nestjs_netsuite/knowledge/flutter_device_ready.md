Making the Flutter app testable on a device:
- Give every tappable element and every value a test reads a stable Key (e.g. Key('cart.line.197.quantity'),
  Key('checkout.continue')); keep semantics labels in step with the visible text for accessibility.
- Never hard-code the API host or port: read API_BASE_URL from --dart-define (staging on the emulator is
  http://10.0.2.2:<staging port>/api/v1).
- Buttons that wait for the server (Continue, Place order) show progress while saving and are enabled from the
  server's confirmed state, not only from what the user tapped; show the API's error message when a save fails.
- Calls to the real site can take 10 to 15 seconds: receive timeout of at least 30 seconds (60 for place order).
- Integration tests wait for the server state (pumpAndSettle with a timeout, or a finder loop) instead of fixed
  delays.
