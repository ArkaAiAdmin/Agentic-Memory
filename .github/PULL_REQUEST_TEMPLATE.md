---
name: Pull Request
about: Contribute code, docs, or tests
title: ""
labels: ""
assignees: ""
---

## What does this PR do?

<!-- One-sentence summary. Link to the issue it closes: "Closes #NNN" -->

## Type of change

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Test addition / fix
- [ ] Refactor (no functional change)

## Checklist

- [ ] I have read [CONTRIBUTING.md](CONTRIBUTING.md)
- [ ] I ran the full test suite locally: `./venv/bin/python -m pytest eval/ -q`
- [ ] New code is covered by a test (or explicitly marked `# noqa: TODO test`)
- [ ] `ruff check .` passes with no new warnings
- [ ] `mypy` passes with no new errors
- [ ] I updated `docs/` if the change affects user-facing behaviour
- [ ] I updated `CHANGELOG.md` under the `[Unreleased]` section

## Test plan

<!-- How did you verify this change? Paste test output or describe what you ran. -->

```
# Example:
# . venv/bin/activate
# pytest eval/test_save_pipeline.py -v
# pytest eval/test_search_quality.py -v
```

## Notes for reviewer

<!-- Anything unusual: DB migration, schema change, environment variable, backwards-compat concern? -->

## Screenshots (if UI / dashboard change)

<!-- Drag-and-drop screenshots here, or link to a recording. -->
