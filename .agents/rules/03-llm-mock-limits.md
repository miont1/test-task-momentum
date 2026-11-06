# Rule: LLM Provider, Rate Limits & Zero-Key Fallback

## Rate Limiting
- Hard limit: 60 requests per minute across the entire application.
- Enforce this either via worker concurrency settings, Redis token bucket, or task delays.

## Zero-Key / Mock Mode
- If API key is missing or set to "mock":
  - The service must still simulate real network and processing overhead.
  - Apply non-blocking sleep with duration between 5.0 and 30.0 seconds.
  - Return a deterministic, high-quality draft response based on the top-matched knowledge base article.
- The rest of the pipeline (operator UI, approval, outbound webhook) must behave identically regardless of whether the LLM is real or mocked.