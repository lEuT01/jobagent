"""Thin wrapper around the Anthropic SDK with forced-JSON tool output."""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic


class LLM:
    def __init__(self, api_key: str | None, model: str, max_retries: int = 3):
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_retries = max_retries
        self.input_tokens = 0
        self.output_tokens = 0

    def structured(self, *, system: str, user: str, schema: dict, name: str, max_tokens: int = 4000) -> Any:
        """Force the model to return JSON matching `schema` via a single tool call."""
        tool = {"name": name, "description": f"Return the {name}.", "input_schema": schema}
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    tools=[tool],
                    tool_choice={"type": "tool", "name": name},
                )
                self.input_tokens += resp.usage.input_tokens
                self.output_tokens += resp.usage.output_tokens
                for block in resp.content:
                    if block.type == "tool_use" and block.name == name:
                        return block.input
                raise RuntimeError("model did not call the tool")
            except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
                last_err = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"LLM call failed after retries: {last_err}")

    def usage_summary(self) -> str:
        return f"{self.model}: {self.input_tokens:,} in / {self.output_tokens:,} out tokens"
