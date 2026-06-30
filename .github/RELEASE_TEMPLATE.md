---
name: Release
about: Track a new agentic-memory release
title: "Release v"
labels: release
---

## Release Checklist

### 1. Version Bump
- [ ] Bump `__version__` in `_version.py` / `pyproject.toml`
- [ ] Update `CHANGELOG.md` (move [Unreleased] → new version)
- [ ] Commit: `chore: bump version to vX.Y.Z`

### 2. Test
- [ ] Full suite: `./venv/bin/python -m pytest eval/ -n 3 --timeout=90 -q`
- [ ] Typecheck: `./venv/bin/python -m mypy .`
- [ ] Lint: `ruff check .`
- [ ] Benchmarks: `./venv/bin/python eval/benchmarks/bench_save.py --quick`
- [ ] Benchmarks: `./venv/bin/python eval/benchmarks/bench_search.py --quick`

### 3. Build & Publish
- [ ] `./venv/bin/python -m build`
- [ ] `twine check dist/*`
- [ ] `twine upload dist/*`
- [ ] Tag: `git tag vX.Y.Z && git push origin vX.Y.Z`

### 4. GitHub Release
- [ ] Create release from tag with changelog notes
- [ ] Attach `dist/*` artifacts

## Versioning

See `CHANGELOG.md` for the version history. This project follows
[Semantic Versioning](https://semver.org/).
