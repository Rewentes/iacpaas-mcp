"""Use public model capabilities to leave room for the review's visible report."""

import time

import httpx

from .review_schema import REVIEW_SCHEMA

_CACHE = {}


def reasoning_options(metadata):
    reasoning = metadata.get("reasoning") or {}
    efforts = reasoning.get("supported_efforts", [])
    if "none" in (efforts or []) and reasoning.get("mandatory") is False:
        return {"reasoning": {"enabled": False}}
    if efforts is None or "low" in efforts:
        return {"reasoning": {"effort": "low"}}
    if "minimal" in efforts:
        return {"reasoning": {"effort": "minimal"}}
    if "none" in efforts and reasoning.get("mandatory") is not True:
        return {"reasoning": {"enabled": False}}
    return {}


def review_options(metadata):
    options = reasoning_options(metadata)
    supported = metadata.get("supported_parameters") or []
    if "structured_outputs" in supported:
        options["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "answer_review", "strict": True, "schema": REVIEW_SCHEMA}}
        options["provider"] = {"require_parameters": True}
    elif "response_format" in supported:
        options["response_format"] = {"type": "json_object"}
        options["provider"] = {"require_parameters": True}
    return options


async def openrouter_review_options(model, client=None):
    cached = _CACHE.get(model)
    if cached and time.monotonic() - cached[0] < 900:
        return cached[1]

    async def fetch(client):
        response = await client.get("https://openrouter.ai/api/v1/models", timeout=10)
        response.raise_for_status()
        metadata = next(
            (item for item in response.json().get("data", []) if item.get("id") == model), {}
        )
        return review_options(metadata)

    try:
        if client is not None:
            options = await fetch(client)
        else:
            async with httpx.AsyncClient(follow_redirects=False) as request_client:
                options = await fetch(request_client)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return {}
    if len(_CACHE) >= 128:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[model] = (time.monotonic(), options)
    return options
