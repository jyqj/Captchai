# Positioning

## What CaptchAI is

CaptchAI is a self-hostable captcha solving service with a YesCaptcha-style API for the task types implemented in this repository.

It is designed for users who want:

- a service they can run themselves
- compatibility with `createTask` / `getTaskResult` style workflows
- control over browser automation and model backends
- support for OpenAI-compatible multimodal providers, including local or self-hosted gateways

## Comparison with managed services such as YesCaptcha

Managed services such as YesCaptcha typically provide:

- a hosted platform
- a broad task catalog
- commercial SLAs and vendor-managed infrastructure

CaptchAI instead focuses on:

- self-hosting
- transparent implementation
- prompt and browser customization
- backend flexibility for multimodal models

## Scope boundary

This repository should not be described as a full commercial-vendor replacement for every captcha family or task type.

A more accurate description is:

> a self-hostable service that provides a YesCaptcha-style API for the implemented task types and can be integrated into systems such as flow2api.

## Local and self-hosted model support

The project uses OpenAI-compatible APIs for multimodal recognition. This makes it possible to connect:

- hosted providers
- internal gateways
- self-hosted multimodal services
- local model-serving stacks that expose compatible semantics

The documentation intentionally keeps this phrasing generic. Compatibility depends on whether the backend supports image input and sufficiently consistent chat-completions behavior.
