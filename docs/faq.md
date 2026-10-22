# FAQ

## Does this fully replace YesCaptcha?

No. It implements a YesCaptcha-style API surface for the task types available in this repository. It should not be described as full vendor parity.

## Does `minScore` guarantee a target reCAPTCHA score?

No. The request model accepts `minScore` for compatibility, but the current solver does not enforce score targeting.

## Can I use local or self-hosted multimodal models?

Yes, if they expose an OpenAI-compatible API with image-capable chat completion behavior.

## Does `ImageToTextTask` return plain OCR text?

Not necessarily. The current implementation returns structured recognition output serialized into `solution.text`.

## Is task state persistent?

By default, no: task state is stored in memory and cleaned up after the 10-minute task TTL. Set `REDIS_URL` (with the optional `redis>=4.2` package) to share the task store, ledger, accounting, proxy pool, and idempotency claims across workers and survive restarts. Per-process state (rate limiting, task admission, warm sessions, model concurrency, budget in-flight reservations) is not shared.

## Why does the service refuse to start with no CLIENT_KEY?

That is the fail-closed auth guard. A keyless bind on a non-loopback `SERVER_HOST` is refused unless you explicitly set `ALLOW_ANONYMOUS=true`. `ALLOW_ANONYMOUS` only relaxes startup; a configured `CLIENT_KEY` is still required on every API call.

## Which endpoint should a load balancer probe?

`GET /ready` for readiness (HTTP 200/503). `GET /api/v1/health` is the always-200 liveness/details endpoint; there is no bare `/health` route.

## What affects reCAPTCHA v3 results?

Common factors include IP quality, browser fingerprint, target site behavior, page action correctness, and runtime environment.
