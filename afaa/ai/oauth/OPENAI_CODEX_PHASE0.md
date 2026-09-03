# OpenAI Codex Phase 0 spike

This live utility proves the current public Codex device flow and Pydantic AI Responses transport without persisting credentials.

```bash
cd /home/frappe/frappe-bench
AFAA_CODEX_LIVE_TEST=1 bench --site <site> execute \
  afaa.ai.openai_codex_phase0.run_phase0_spike \
  --kwargs '{"model_id":"<codex-model-slug>","acknowledge_policy_risk":true}'
```

The command prints only the verification URL and one-time user code. Access, refresh, ID, authorization, device authorization, and PKCE values remain in memory and are excluded from dataclass representations. The rotated refresh token is revoked before the command exits. A successful result reports device authorization, exchange, metadata extraction, refresh/rotation, plain text, structured output, tool use, multi-turn reasoning, and an intentional 401 probe. It does not force subscription exhaustion; a 429 can only be marked observed when the tested account is actually limited.

The live spike is excluded from normal CI and requires `AFAA_CODEX_LIVE_TEST=1`. Override the public client through site config when an approved client registration is available:

```json
{
  "afaa_openai_codex_client_id": "approved-client-id",
  "afaa_openai_codex_spike_model": "model-slug"
}
```

## Release policy gate

Do not release subscription OAuth to production until written policy confirmation covers all of these items:

- AFAA may use the Codex public OAuth client and device flow.
- `originator: afaa` is accepted.
- Server-side AFAA execution is permitted for each intended account type.
- Subscription sharing rules between Frappe users are defined.

The public client ID in OpenAI's repository is protocol evidence, not service authorization.
