# Tests

Unit tests for core logic in `memory_wrapper.py` that can run without a live database.

These cover pure functions — platitude filtering, log extraction, agent schema mapping,
and JSONL parsing. No PostgreSQL, Neo4j, or Ollama required.

**Integration testing** is handled by the live system itself. The agents run hourly on
real data at clawbr.org. If wake/sleep cycles produce meaningful memories and the
Q-value scores shift over time, the system is working.

## Run

```bash
cd drift-agents
pip install pytest
pytest tests/ -v
```
