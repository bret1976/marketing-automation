# Content Team LLM options ($0)

Checked 16 November 2026 against official pages. Goal: 20–40 short writing calls a day, no new Railway RAM, no paid APIs.

## Recommendation

**Primary: Cloudflare Workers AI** (`@cf/zai-org/glm-4.7-flash`) on Bret's existing Cloudflare account, Workers Free plan. Usage hard-stops at 10,000 Neurons/day instead of billing. **Optional fallbacks, only if keys exist:** Groq Free (`openai/gpt-oss-20b`), then OpenRouter `:free`.

This is an OpenAI-compatible endpoint:

```
https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1/chat/completions
```

Set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` (token template "Workers AI", permissions Workers AI: Read and Workers AI: Edit). Stay on Workers Free. Or override with `CONTENT_TEAM_LLM_BASE_URL`, `CONTENT_TEAM_LLM_MODEL`, and `CONTENT_TEAM_LLM_API_KEY`.

Do not deploy this branch until those keys are set and Bret says yes to deploy. Do not add a Workers Paid plan.

## Why this model

Catalog and pricing checked 16 Nov 2026 ([Workers AI models](https://developers.cloudflare.com/workers-ai/models/), [pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)):

| Candidate | In current catalog? | Neurons / 1M in | Neurons / 1M out | ~Neurons per short call (1,500 in + 300 out) | 40 short calls | 60 calls at max_tokens=3,072 and 4,000-token prompts | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `@cf/zai-org/glm-4.7-flash` | Yes. Explicitly still on Workers Free. 131k context, instruction-following. | 5,500 | 36,400 | ~19 | ~767 | ~8,029 | **Default.** Best quality that still fits the free allowance with margin. |
| `@cf/meta/llama-3.2-3b-instruct` | Yes | 4,625 | 30,475 | ~16 | ~640 | ~6,720 | Optional cheaper override. Weaker for Bret's voice. |
| `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | Yes | 26,668 | 204,805 | ~101 | ~4,056 | ~39,300 (over) | Rejected as default. One long 30-idea researcher job is ~600–800 neurons; a 60-call day at max tokens blows past 10k and would 429 into placeholders. |
| `@cf/meta/llama-3.1-8b-instruct-fast` and `@cf/meta/llama-3.1-8b-instruct-fp8-fast` | No. Both model pages 404; not in the current catalog. `llama-3.1-8b-instruct-fp8` remains but is planned for deprecation on 20 Nov 2026. | — | — | — | — | — | Not used. |

A typical short X draft on GLM-4.7-Flash is about **19 neurons**. A heavier researcher/chief JSON job (about 2,500 in + 3,000 out) is about **123 neurons**. Twenty to forty mixed calls land around 1,000–2,000 neurons, well under 10,000. `CONTENT_TEAM_LLM_DAILY_CAP` defaults to 60, and each call sets `max_tokens=3072`, so even a worst-case day stays under the free cap (~8,029 neurons). After the cap, or on a 429 / quota error, the app writes the labeled placeholder and does not try a paid route.

## Comparison

| Option | Ongoing free? | Limits | Card required? | Account | Railway RAM / cost | Fit for 20–40 calls/day |
| --- | --- | --- | --- | --- | --- | --- |
| Cloudflare Workers AI (`@cf/zai-org/glm-4.7-flash`) | Yes, 10,000 Neurons/day, resets 00:00 UTC | Workers Free hard-stops after the allocation. Some frontier models need a paid method — we do not use those (no GLM-5, Kimi K2.6+, DeepSeek V4, Inkling). | No for Workers Free | Existing Cloudflare account + API token | None (hosted) | **Primary.** Quality is strong; neuron math fits with margin. |
| Groq Free (`openai/gpt-oss-20b` or `gpt-oss-120b`) | Yes, no expiry stated | 30 RPM, 1,000 RPD, 8K TPM, 200K TPD | No (card only to upgrade to Developer) | Email at console.groq.com | None (hosted) | Optional fallback if a key is set. Groq blocked our bot sign-up. |
| OpenRouter `:free` / `openrouter/free` | Yes, if you never buy credits or list a paid fallback | 20 RPM, **50 RPD** with $0 lifetime credits; 1,000 RPD only after buying $10 in credits | No for free models | Email at openrouter.ai | None | Tight (50/day). Optional third fallback only. Never list a paid model in the fallback array. |
| Cerebras | No. One-time $5 trial, expires in 30 days | Trial limits while credits last | Yes — a verified payment method is required before the trial starts | cloud.cerebras.ai | None | Rejected. Not ongoing $0. |
| Hugging Face Inference Providers | No. Free users get $0 monthly credits | Pay as you go after purchase, or PRO at $9/mo for $2 credits | Credits or PRO must be paid | huggingface.co | None | Rejected. |
| GitHub Models | Retired 30 Jul 2026 | n/a | n/a | n/a | n/a | Not available. |
| Self-host Qwen2.5 0.5B–1.5B or Llama 3.2 1B/3B GGUF in the Railway container | $0 model weights, but RAM is billed | N/A | N/A | N/A | ~0.6 GB / ~$6–10/mo (0.5B); ~1.4 GB / ~$14/mo (1B); ~2.5–3.5 GB / ~$25–35/mo (3B) at ~$10/GB-month. Quality of 0.5B–1B is weak for Bret's voice. | Rejected for production. The Railway bill was just cut to about $7.56/mo. |
| Ollama on the Mac Mini via a tunnel | Yes, when the Mac is awake | Machine-local | No | Local Ollama | None on Railway | Optional only. 7 AM PT jobs fail if the Mac is off. Set `CONTENT_TEAM_LLM=openai` and `CONTENT_TEAM_LLM_BASE_URL` to the tunnel when you want it. |

Sources (official, retrieved 16 Nov 2026):

- https://developers.cloudflare.com/workers-ai/platform/pricing/
- https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/
- https://developers.cloudflare.com/workers-ai/models/glm-4.7-flash/
- https://developers.cloudflare.com/workers-ai/models/
- https://console.groq.com/docs/rate-limits
- https://console.groq.com/docs/billing-faqs
- https://console.groq.com/settings/billing/plans
- https://openrouter.ai/docs/api_reference/limits
- https://openrouter.ai/docs/guides/routing/model-variants/free
- https://openrouter.ai/pricing
- https://inference-docs.cerebras.ai/console/account-billing
- https://huggingface.co/docs/inference-providers/pricing
- https://github.blog/changelog/2026-07-30-github-models-is-now-retired/
