# BEAM Benchmark for agentic-memory

BEAM (Board of Evaluation for Agent Memory) tests long-context memory tracking with synthetic conversations that evolve over time.

## What it Measures

- **Fact tracking accuracy**: Can the system recall the current value of facts that change over time?
- **Temporal awareness**: Does the system return the most recent version of facts?
- **Scale performance**: How does accuracy degrade at 100K, 1M, and 10M token scales?

## Quick Start

```bash
# Run at smallest scale (100K tokens)
venv/bin/python eval/beam/run_beam_eval.py --scale 100K

# Run at 1M scale
venv/bin/python eval/beam/run_beam_eval.py --scale 1M

# Run at all scales
venv/bin/python eval/beam/run_beam_eval.py --all-scales
```

## Results

| Scale | agentic-memory | Cognee | Mem0 |
|-------|----------------|--------|------|
| 100K  | 100.00%        | 79.00% | -    |
| 1M    | 88.89%         | -      | 64.1 |

**Published baselines**: Cognee 0.79 at 100K, Mem0 64.1 at 1M

## Configuration

Edit `SCALES` dict in `run_beam_eval.py` to adjust:
- `token_budget`: Target token count
- `sessions`: Number of synthetic sessions
- `session_tokens`: Approximate tokens per session

## Output

Results are saved to `eval/beam/results/beam-run.json` with:
- Per-question accuracy scores
- Latency measurements
- Comparison with published baselines

## Methodology

1. **Data generation**: Creates N sessions with 8 evolving facts (favorite color, project status, team size, budget, tech stack, deadline, coffee order, meeting time)
2. **Ingestion**: Saves all sessions to a SQLite database with FTS5 indexing
3. **Evaluation**: For each fact, queries the system and checks if the most recent value is returned
4. **Scoring**: 1.0 if expected value is found in top result, 0.0 otherwise
