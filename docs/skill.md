# Agent Skill

CaptchAI ships with reusable skills under `skills/`.

## Available skills

- `skills/captchai/` — operate, deploy, validate, and integrate the service
- `skills/captchai-image/` — create public-safe visuals for README, docs, and launch assets

## For humans

If your tool can read a local skill folder directly, copy one or both of these directories into your local skills directory:

```text
skills/captchai/
skills/captchai-image/
```

Then restart your tool if it caches skill metadata.

## Let an LLM do it

Paste this into any capable agent environment:

```text
Install the CaptchAI skills from this repository and make them available in my local skills directory. Then show me how to use the operational skill for deployment and the image skill for generating README or docs visuals.
```

## What the operational skill does

The `captchai` skill helps with:

- local startup
- environment setup
- YesCaptcha-style API usage
- flow2api integration
- Render deployment
- Hugging Face Spaces deployment
- task validation and troubleshooting

## What the image skill does

The `captchai-image` skill helps with:

- README hero image prompts
- docs illustrations
- public-safe technical visuals
- architecture-themed artwork
- reusable image-generation prompts for agent workflows

## Design guarantees

These skills are designed to:

- use placeholder credentials only
- stay aligned with the implemented task types
- keep current limitations explicit
- avoid embedding secrets, private endpoints, or customer data
