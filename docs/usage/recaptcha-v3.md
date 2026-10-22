# reCAPTCHA v3 Usage

## Target used for acceptance

This repository was validated against:

- URL: `https://antcpt.com/score_detector/`
- site key: `6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf`

## Create a task

```bash
curl -X POST http://localhost:8000/createTask \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "task": {
      "type": "RecaptchaV3TaskProxyless",
      "websiteURL": "https://antcpt.com/score_detector/",
      "websiteKey": "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf",
      "pageAction": "homepage"
    }
  }'
```

## Poll for result

```bash
curl -X POST http://localhost:8000/getTaskResult \
  -H "Content-Type: application/json" \
  -d '{
    "clientKey": "your-client-key",
    "taskId": "uuid-from-createTask"
  }'
```

When the task is ready, you should receive `solution.gRecaptchaResponse`.

## Acceptance result for this codebase

A local acceptance run against the public detector target successfully:

- started the service
- created a task
- reached `ready`
- returned a non-empty token

## URL validation

Before a proxy or browser context is checked out, `websiteURL` must be http(s), the hostname must resolve, and every resolved address must be public (loopback, private, link-local, reserved, multicast, and unspecified are rejected). This pre-flight is always on for reCAPTCHA v3; a rejected URL surfaces as `ERROR_CAPTCHA_UNSOLVABLE`.

## Operational caveats

- A returned token does not imply guaranteed score targeting.
- Site behavior may vary over time.
- IP quality and browser environment can affect outcomes.
- The repository currently uses the same internal solver path for all registered reCAPTCHA v3 task variants.
