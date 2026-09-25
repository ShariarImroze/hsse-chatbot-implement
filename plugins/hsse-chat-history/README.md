# HSSE Chat History Codex plugin

This local, read-only plugin exposes conversations saved by the HSSE chatbot to
Codex through three MCP tools: list, retrieve, and search. Conversation content
stays in `data/local/user_cases.sqlite`; the plugin does not read the 400K corpus.

The chatbot starts recording exchanges automatically after the updated app is
launched. Existing browser-local history is imported when the page is reopened.
