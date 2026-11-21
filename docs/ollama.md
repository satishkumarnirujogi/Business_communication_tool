# Using a local model with Ollama

With [Ollama](https://ollama.com) the language model runs on your own machine. Patient messages never go to an
AI company, and there is no per-message cost. The knowledge base already uses a local embedding model by default,
so with Ollama the whole assistant works offline.

The trade-off is quality and speed. Small local models follow instructions less reliably than large hosted ones.
The safety rules are enforced in code, so a weaker model can't skip the confirmation step or emergency handling,
but it may phrase things awkwardly or need an extra turn. Test it with the demo clinic before going live.

## What you need

| Model size | RAM / GPU | Experience |
|---|---|---|
| 7–8B (e.g. `qwen3:8b`, `llama3.1:8b`) | 8 GB RAM; a GPU with 8 GB makes replies fast | Works for booking and FAQ; slow on CPU only (10–30 s per reply) |
| 14B (e.g. `qwen3:14b`) | 16 GB RAM or a 12 GB GPU | Noticeably better at multi-step bookings |
| 30B+ | 24 GB+ GPU | Close to hosted models |

Pick a model that supports **tool calling** (Ollama marks these with a "tools" tag). Qwen 3 handles German and
English well.

## 1. Install Ollama and pull a model

Install from [ollama.com/download](https://ollama.com/download), then:

```bash
ollama pull qwen3:8b
ollama run qwen3:8b "Say hello in German"     # quick check
```

## 2. Point the assistant at it

In `.env`, or in **Admin → Settings → AI model**, which has a **Test connection** button:

```env
LLM_PROVIDER=ollama
LLM_MODEL=qwen3:8b
LLM_BASE_URL=http://localhost:11434
LLM_API_KEY=
LLM_TOOL_MODE=auto
```

**With Docker**, the assistant runs in a container, where `localhost` is the container itself. Use the host
instead (`docker-compose.yml` maps this name on Linux too):

```env
LLM_BASE_URL=http://host.docker.internal:11434
```

On Linux, Ollama listens only on 127.0.0.1 by default. Make it reachable from containers with
`sudo systemctl edit ollama`: add `Environment="OLLAMA_HOST=0.0.0.0"` under `[Service]`, then
`sudo systemctl restart ollama`. Keep port 11434 closed in your firewall.

## 3. If tool calls go wrong

`LLM_TOOL_MODE=auto` uses the model's native tool calling, and switches to JSON mode by itself if Ollama
rejects tools for that model. If the model calls tools with broken arguments or describes a booking instead of
making it, force JSON mode:

```env
LLM_TOOL_MODE=json
```

In JSON mode the tools are described in the prompt and the model replies in a strict JSON format. The assistant
asks for one correction if the JSON is broken. Both modes go through the same rule checks and audit log.

## 4. Running Ollama on another machine

A small office server can run the assistant while a GPU machine runs Ollama:

```env
LLM_BASE_URL=http://192.168.1.50:11434
```

Only do this on a trusted network. Ollama has no authentication, so never expose port 11434 to the internet.

## Troubleshooting

| Message | Fix |
|---|---|
| "Can't reach http://localhost:11434" (in Docker) | Use `http://host.docker.internal:11434`, and on Linux set `OLLAMA_HOST=0.0.0.0` |
| "Model '…' was not found at ollama" | Run `ollama pull <model>` and use the exact name from `ollama list` |
| "… did not answer within 60 seconds" | Raise `LLM_TIMEOUT_SECONDS` (e.g. 180) or use a smaller model / a GPU |
| Model answers in the wrong language or ignores rules | Try a larger model, or `LLM_TOOL_MODE=json` |
