# CaptchAI Skills

Reusable skills for Claude Code, Cursor, OpenCode, and similar agent environments.

## Included skills

| Skill | Purpose |
|-------|---------|
| `skills/captchai/` | Deploy, validate, and integrate CaptchAI (local model setup, API usage, deployment) |
| `skills/captchai-image/` | Generate public-safe visuals for README and documentation |

## Installation

Copy the skill folder(s) into your skills directory:

```bash
# For Cursor
cp -r skills/captchai ~/.cursor/skills/

# For project-scoped usage
cp -r skills/captchai .cursor/skills/
```

## Key concepts

The operational skill (`captchai`) covers:
- **Local model deployment** — SGLang/vLLM serving Qwen3.5-2B for image tasks
- **Cloud model configuration** — remote API (gpt-5.4) for audio transcription
- **19 task types** — reCAPTCHA v2/v3, hCaptcha, Turnstile, image classification
- **Deployment** — local, Render, Hugging Face Spaces

The image skill (`captchai-image`) provides prompting guidance for generating repository art and documentation visuals.
