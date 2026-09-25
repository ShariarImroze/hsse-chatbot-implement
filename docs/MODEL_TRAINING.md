# HSSE model training

The chatbot keeps exact corpus lookup in SQLite. Fine-tuning is limited to the
separate task of classifying a newly supplied incident; it is not a substitute
for retrieval and should not be used to memorize 400,000 records.

## Export the supervised dataset

```bash
python scripts/export_sft_dataset.py
```

This writes chat-format JSONL files to `data/training/hsse_sft/` using the
existing train, validation, and test assignments (319,598 / 40,255 / 40,147).
For a quick pipeline test, add `--limit-per-split 100`.

The labels include deterministic weak labels. Always report held-out metrics
per field, compare them with a no-training baseline, and inspect errors by data
source before deploying an adapter.

## Train adapters, not full copies

Train one LoRA/QLoRA adapter per base model with a CUDA training service. Keep
the three base model licenses and chat templates separate. Suggested run order:

1. Meta Llama 3.1 8B Instruct
2. Mistral 7B Instruct v0.3

The UI expects OpenAI-compatible llama.cpp endpoints on ports 8080 and 8082:

```bash
llama-server -hf bartowski/Meta-Llama-3.1-8B-Instruct-GGUF:Q4_K_M --port 8080
llama-server -hf bartowski/Mistral-7B-Instruct-v0.3-GGUF:Q4_K_M --port 8082
```

For one-machine testing, stop one model before starting another. The buttons
remain useful because an endpoint may also point to another workstation or a
GPU server.
