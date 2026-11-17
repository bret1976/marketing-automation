# Content Team LLM options ($0)

Checked 16 November 2026 against official pages. Goal: 20–40 short writing calls a day, no new Railway RAM, no paid APIs.

## Recommendation

**Primary: Groq Free** (`openai/gpt-oss-20b`). **Fallback: Cloudflare Workers AI** (`@cf/meta/llama-3.2-3b-instruct`), using Bret's existing Cloudflare account, on the Workers Free plan so usage stops at the daily neuron cap instead of billing.

Both are OpenAI-compatible. Sign-ups needed (do not buy anything):

- Groq: https://console.groq.com — email account, no card for the Free plan. Create `GROQ_API_KEY`.
- Cloudflare: existing account. Create a token with Workers AI permission. Set `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`. Stay on Workers Free.

Do not deploy this branch until those keys are set and Bret says yes to deploy.

## Comparison

| Option | Ongoing free? | Limits | Card required? | Account | Railway RAM / cost | Fit for 20–40 calls/day |
| --- | --- | --- | --- | --- | --- | --- |
| Groq Free (`openai/gpt-oss-20b` or `gpt-oss-120b`) | Yes, no expiry stated | 30 RPM, 1,000 RPD, 8K TPM, 200K TPD | No (card only to upgrade to Developer) | Email at console.groq.com | None (hosted) | Best. Quality is high; headroom is large. |
| Cloudflare Workers AI | Yes, 10,000 Neurons/day, resets 00:00 UTC | Workers Free hard-stops after the allocation. Llama 3.2 3B uses a few dozen neurons per short call, so 20–40 calls stay well under 10k. Some frontier models need a paid method — we do not use those. | No for Workers Free | Existing Cloudflare account + API token | None (hosted) | Good fallback. |
| OpenRouter `:free` / `openrouter/free` | Yes, if you never buy credits or list a paid fallback | 20 RPM, **50 RPD** with $0 lifetime credits; 1,000 RPD only after buying $10 in credits | No for free models | Email at openrouter.ai | None | Tight (50/day). Optional third fallback only. Never list a paid model in the fallback array. |
| Cerebras | No. One-time $5 trial, expires in 30 days | Trial limits while credits last | Yes — a verified payment method is required before the trial starts | cloud.cerebras.ai | None | Rejected. Not ongoing $0. |
| Hugging Face Inference Providers | No. Free users get $0 monthly credits | Pay as you go after purchase, or PRO at $9/mo for $2 credits | Credits or PRO must be paid | huggingface.co | None | Rejected. |
| GitHub Models | Retired 30 Jul 2026 | n/a | n/a | n/a | n/a | Not available. |
| Self-host Qwen2.5 0.5B–1.5B or Llama 3.2 1B/3B GGUF in the Railway container | $0 model weights, but RAM is billed | N/A | N/A | N/A | ~0.6 GB / ~$6–10/mo (0.5B); ~1.4 GB / ~$14/mo (1B); ~2.5–3.5 GB / ~$25–35/mo (3B) at ~$10/GB-month. Quality of 0.5B–1B is weak for Bret's voice. | Rejected for production. The Railway bill was just cut to about $7.56/mo. |
| Ollama on the Mac Mini via a tunnel | Yes, when the Mac is awake | Machine-local | No | Local Ollama | None on Railway | Optional only. 7 AM PT jobs fail if the Mac is off. Set `CONTENT_TEAM_LLM_BASE_URL` to the tunnel when you want it. |

Sources (official, retrieved 16 Nov 2026):

- https://console.groq.com/docs/rate-limits
- https://console.groq.com/docs/billing-faqs
- https://console.groq.com/settings/billing/plans
- https://developers.cloudflare.com/workers-ai/platform/pricing/
- https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/
- https://openrouter.ai/docs/api_reference/limits
- https://openrouter.ai/docs/guides/routing/model-variants/free
- https://openrouter.ai/pricing
- https://inference-docs.cerebras.ai/console/account-billing
- https://huggingface.co/docs/inference-providers/pricing
- https://github.blog/changelog/2026-07-30-github-models-is-now-retired/
