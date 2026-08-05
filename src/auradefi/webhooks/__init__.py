"""Signed, durable, replayable webhooks (SPEC §7.3, rule #8).

HMAC-SHA256 over timestamp + body, delivery_id, exponential backoff over
24h, a dead-letter view and a replay path. Vezgo authenticates webhooks
by source-IP allowlist and Zerion retries 3x over 60s then drops; neither
failure mode exists here. Delivery is host-scheduled: Deliverer.tick(now_ms)
drains due deliveries through an injected httpx client.
"""
