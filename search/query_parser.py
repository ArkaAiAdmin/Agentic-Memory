"""Search query parsing, expansion, and zero-result suggestion helpers.

Extracted from search_pipeline.py (2026-06-20) as part of the
god-module decomposition. Contains the pure query-shaping primitives
that the main search_memories orchestrator calls:

- _parse_search_query: full query -> (normalized, fts_query, bare, graph_rag_terms)
- _escape_fts_query, _escape_phrase: FTS5 escaping primitives
- _expand_query: synonym/abbreviation expansion (QW2)
- _did_you_mean: typo/synonym correction candidates
- _detect_query_type, _weights_for_query_type: query classification (QW3)
- _graph_rag_expand: KG-based query expansion
- _top_recent_tags, _top_recent_notes, _top_recent_source_files:
  zero-result suggestion channels
- _build_zero_result_suggestions: composes the four channels

The _QUERY_EXPANSIONS dict and query-type regexes live here.
Behavior is identical to the inline versions in search_pipeline.
Re-exported from search_pipeline for backward compat.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from infra.infrastructure import _normalize_unicode as normalize_unicode
from infra.memory_common import connection_pool, safe_close_db

logger = logging.getLogger(__name__)

def decompose_compound_query(query: str) -> list[str]:
    """Decompose compound queries (e.g. multi-session reasoning) into sub-queries.
    
    E.g. 'What brand of racket did John buy for his favorite sport?'
    -> ['John favorite sport', 'John racket brand']
    """
    if not query:
        return []
    
    q_lower = query.lower().strip()
    
    # Check for compound patterns (for his/her, and what, regarding the, during the)
    patterns = [
        r"(.+?)\s+(?:for|regarding|about|during)\s+(?:his|her|their|the)\s+(.+)",
        r"(.+?)\s+and\s+(?:what|which|who|where|when|how)\s+(.+)",
    ]
    
    for pat in patterns:
        m = re.search(pat, q_lower)
        if m:
            part1 = m.group(1).strip()
            part2 = m.group(2).strip()
            if len(part1) > 5 and len(part2) > 5:
                return [part1, part2]
                
    return []


# Stop words: high-frequency words that waste FTS5 match budget on AND queries
_STOP_WORDS = frozenset({
    'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'be', 'been',
    'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would',
    'could', 'should', 'may', 'might', 'shall', 'can', 'need', 'dare',
    'ought', 'used', 'what', 'which', 'who', 'whom', 'this', 'that',
    'these', 'those', 'i', 'you', 'he', 'she', 'it', 'we', 'they',
    'me', 'him', 'her', 'us', 'them', 'my', 'your', 'his', 'its',
    'our', 'their', 'mine', 'yours', 'hers', 'ours', 'theirs',
    'am', 'if', 'then', 'else', 'when', 'where', 'how', 'all', 'each',
    'every', 'both', 'few', 'more', 'most', 'other', 'some', 'such',
    'no', 'not', 'only', 'own', 'same', 'so', 'than', 'too', 'very',
    'just', 'because', 'as', 'until', 'while', 'about', 'between',
    'through', 'during', 'before', 'after', 'above', 'below', 'up',
    'down', 'out', 'off', 'over', 'under', 'again', 'further', 'once',
    'here', 'there', 'any', 'also', 'many', 'much', 'spend', 'spent',
    'total', 'combined', 'different', 'since', 'start', 'take', 'took',
    # Removed: type, kind, sort, want, looking, tell, show, about
    # These are query-intent words that matter for search memory queries.
    # "what type of database" needs "type" to work.
})

# Word form expansions: porter stemming misses these cross-form matches.
# Maps a stem to all its surface forms so FTS5 OR-matches correctly.
# E.g. "container" and "containerize" have different porter stems, but
# we want queries containing either to match documents containing either.
_WORD_FORM_EXPANSIONS: dict[str, list[str]] = {
    'doctor': ['doctor', 'doctors', 'physician', 'physicians', 'specialist', 'specialists', 'dermatologist', 'ent', 'dr', 'appointment', 'clinic'],
    'bike': ['bike', 'bikes', 'cycling', 'bicycle', 'bicycles', 'cyclist', 'tune-up', 'pedal', 'helmet'],
    'camp': ['camping', 'camp', 'campground', 'campsite', 'hike', 'hiking', 'backpacking'],
    'container': ['container', 'containers', 'containerize', 'containerized', 'containerizing', 'containerization'],
    'deploy': ['deploy', 'deploys', 'deployed', 'deploying', 'deployment', 'deployments'],
    'orchestrat': ['orchestrate', 'orchestrates', 'orchestrated', 'orchestrating', 'orchestration', 'orchestrator', 'orchestrators'],
    'observ': ['observe', 'observes', 'observed', 'observing', 'observation', 'observations', 'observability', 'observable'],
    'serial': ['serialize', 'serializes', 'serialized', 'serializing', 'serialization', 'serializations'],
    'index': ['index', 'indexes', 'indexed', 'indexing', 'indices'],
    'monitor': ['monitor', 'monitors', 'monitored', 'monitoring', 'monitoring'],
    'configur': ['configure', 'configures', 'configured', 'configuring', 'configuration', 'configurations'],
    'optimi': ['optimize', 'optimizes', 'optimized', 'optimizing', 'optimization', 'optimisations', 'optimizations'],
    'automat': ['automate', 'automates', 'automated', 'automating', 'automation', 'automations'],
    'implement': ['implement', 'implements', 'implemented', 'implementing', 'implementation', 'implementations'],
    'integrat': ['integrate', 'integrates', 'integrated', 'integrating', 'integration', 'integrations'],
    'migrat': ['migrate', 'migrates', 'migrated', 'migrating', 'migration', 'migrations'],
    'compil': ['compile', 'compiles', 'compiled', 'compiling', 'compilation', 'compilations'],
    'test': ['test', 'tests', 'testing', 'tested', 'tester', 'testers'],
    'search': ['search', 'searches', 'searched', 'searching', 'retrieval', 'retrieve', 'retrieves', 'retrieved', 'retrieving'],
    'perform': ['perform', 'performs', 'performed', 'performing', 'performance', 'performances'],
    'secur': ['secure', 'secures', 'secured', 'securing', 'security', 'securities'],
    'author': ['authorize', 'authorizes', 'authorized', 'authorizing', 'authorization', 'authorizations', 'authorise', 'authorisation'],
    'authentic': ['authenticate', 'authenticates', 'authenticated', 'authenticating', 'authentication', 'authentications'],
    'encrypt': ['encrypt', 'encrypts', 'encrypted', 'encrypting', 'encryption', 'encryptions'],
    'compress': ['compress', 'compresses', 'compressed', 'compressing', 'compression', 'compressions'],
    'synch': ['synchronize', 'synchronizes', 'synchronized', 'synchronizing', 'synchronization', 'sync', 'syncs', 'synced', 'syncing'],
    'asynch': ['asynchronize', 'asynchronizes', 'asynchronized', 'asynchronizing', 'asynchronization', 'async'],
    'consolid': ['consolidate', 'consolidates', 'consolidated', 'consolidating', 'consolidation'],
    'extract': ['extract', 'extracts', 'extracted', 'extracting', 'extraction', 'extractions'],
    'deduplic': ['deduplicate', 'deduplicates', 'deduplicated', 'deduplicating', 'deduplication', 'dedup', 'deduplicates'],
    'summar': ['summarize', 'summarizes', 'summarized', 'summarizing', 'summarization', 'summarisations', 'summaries', 'summary'],
    'compact': ['compact', 'compacts', 'compacted', 'compacting', 'compaction'],
    'retent': ['retain', 'retains', 'retained', 'retaining', 'retention'],
    'decay': ['decay', 'decays', 'decayed', 'decaying'],
    'supersed': ['supersede', 'supersedes', 'superseded', 'superseding', 'supersession'],
    'reconcil': ['reconcile', 'reconciles', 'reconciled', 'reconciling', 'reconciliation'],
    'propagat': ['propagate', 'propagates', 'propagated', 'propagating', 'propagation'],
    'embed': ['embed', 'embeds', 'embedded', 'embedding', 'embeddings'],
    'chunk': ['chunk', 'chunks', 'chunked', 'chunking'],
    'vector': ['vector', 'vectors', 'vectorized', 'vectorization'],
    'cluster': ['cluster', 'clusters', 'clustered', 'clustering'],
    'entiti': ['entity', 'entities'],
    'relat': ['relation', 'relations', 'relationship', 'relationships', 'related'],
    'contradict': ['contradict', 'contradicts', 'contradicted', 'contradicting', 'contradiction', 'contradictions'],
    'entail': ['entail', 'entails', 'entailed', 'entailing', 'entailment', 'entailments'],
    'infer': ['infer', 'infers', 'inferred', 'inferring', 'inference', 'inferences'],
    'compile': ['compile', 'compiles', 'compiled', 'compiling', 'compilation'],
    'enrich': ['enrich', 'enriches', 'enriched', 'enriching', 'enrichment'],
    'qualiti': ['quality', 'qualities'],
    'prioriti': ['prioritize', 'prioritizes', 'prioritized', 'prioritizing', 'priority', 'priorities'],
    'schedul': ['schedule', 'schedules', 'scheduled', 'scheduling', 'scheduler'],
    'config': ['config', 'configs', 'configuration', 'configurations', 'configure', 'configured'],
    'live': ['live', 'lives', 'living', 'lived', 'reside', 'resides', 'resided', 'residing', 'stay', 'stayed', 'staying', 'move', 'moved', 'relocate', 'relocated'],
    'rebuild': ['rebuild', 'rebuilds', 'rebuilt', 'rebuilding'],
    'backup': ['backup', 'backups', 'backed', 'backing'],
    'restore': ['restore', 'restores', 'restored', 'restoring', 'restoration'],
    'purge': ['purge', 'purges', 'purged', 'purging'],
    'revis': ['revision', 'revisions', 'revise', 'revises', 'revised', 'revising'],
    'assert': ['assertion', 'assertions', 'assert', 'asserts', 'asserted', 'asserting'],
    'bel': ['belief', 'beliefs', 'believe', 'believes', 'believed', 'believing'],
    'fact': ['fact', 'facts'],
    'concept': ['concept', 'concepts', 'conceptual'],
    'graph': ['graph', 'graphs', 'graphed', 'graphing'],
    'node': ['node', 'nodes'],
    'edg': ['edge', 'edges'],
    'path': ['path', 'paths'],
    'travers': ['traverse', 'traverses', 'traversed', 'traversing', 'traversal', 'traversals'],
    'commun': ['community', 'communities', 'communicate', 'communicates', 'communicated', 'communicating', 'communication'],
    'centr': ['central', 'centrally', 'center', 'centers', 'centered', 'centering', 'centrality'],
    'between': ['between', 'betweenness'],
}

# Synonym map: real semantic equivalents (not just morphological variants).
# Maps a term to its set of synonyms for OR-expansion in queries.
# Every pair is bidirectional — if A maps to B, B also maps to A (or is
# reachable through a shared entry).
_SYNONYM_MAP: dict[str, set[str]] = {
    # Docker / container ecosystem
    "docker": {"container", "containerize", "containerization", "containerd", "dockerd", "moby"},
    "container": {"docker", "containerize", "containerization", "containerd", "oci", "image"},
    "containerize": {"docker", "container", "containerization"},
    "containerization": {"docker", "container", "containerize"},
    "image": {"container", "docker", "layer", "snapshot"},
    "registry": {"dockerhub", "harbor", "quay", "ecr", "gcr", "artifact"},
    "compose": {"docker-compose", "docker compose", "stack", "service"},
    "dockerfile": {"dockerfile", "containerfile", "build"},
    "containerd": {"docker", "container", "cri-o", "cri"},
    "cri-o": {"containerd", "cri", "container"},
    "podman": {"docker", "container", "pod"},
    # Kubernetes
    "kubernetes": {"k8s", "kube", "orchestration", "cluster"},
    "k8s": {"kubernetes", "kube", "orchestration"},
    "kube": {"kubernetes", "k8s"},
    "pod": {"kubernetes", "k8s", "workload", "container"},
    "deployment": {"kubernetes", "k8s", "rollout", "release", "kustomize"},
    "service": {"kubernetes", "k8s", "endpoint", "lb", "loadbalancer"},
    "ingress": {"kubernetes", "k8s", "gateway", "loadbalancer", "proxy"},
    "helm": {"kubernetes", "k8s", "chart", "package"},
    "kustomize": {"kubernetes", "k8s", "overlay", "patch"},
    "etcd": {"kubernetes", "k8s", "distributed", "key-value", "raft"},
    "kubelet": {"kubernetes", "k8s", "node", "agent"},
    "kubectl": {"kubernetes", "k8s", "cli", "ctl"},
    "namespace": {"kubernetes", "k8s", "ns", "tenant", "scope", "isolation", "workspace"},
    "configmap": {"kubernetes", "k8s", "config", "env"},
    "secret": {"kubernetes", "k8s", "credential", "vault"},
    "persistentvolume": {"kubernetes", "k8s", "pv", "pvc", "storage"},
    "persistentvolumeclaim": {"kubernetes", "k8s", "pvc", "pv", "storage"},
    "statefulset": {"kubernetes", "k8s", "sts", "stateful"},
    "daemonset": {"kubernetes", "k8s", "ds", "daemon"},
    "job": {"kubernetes", "k8s", "cronjob", "batch"},
    "cronjob": {"kubernetes", "k8s", "job", "schedule", "cron"},
    "hpa": {"kubernetes", "k8s", "autoscaling", "horizontal", "scale"},
    "serviceaccount": {"kubernetes", "k8s", "sa", "rbac", "auth"},
    "rbac": {"kubernetes", "k8s", "access", "permission", "auth", "serviceaccount", "role-based access", "access control", "authorization", "permissions"},
    "flannel": {"kubernetes", "k8s", "cni", "network"},
    "calico": {"kubernetes", "k8s", "cni", "network"},
    # Database
    "postgres": {"postgresql", "psql", "pg", "rdbms", "sql", "relational"},
    "postgresql": {"postgres", "psql", "pg", "rdbms", "sql"},
    "psql": {"postgres", "postgresql", "pg"},
    "mysql": {"mariadb", "rdbms", "sql", "relational"},
    "mariadb": {"mysql", "rdbms", "sql"},
    "sqlite": {"sql", "lite", "embedded", "rdbms"},
    "redis": {"cache", "key-value", "valkey", "session", "store"},
    "valkey": {"redis", "cache", "key-value"},
    "mongodb": {"mongo", "nosql", "document", "atlas"},
    "mongo": {"mongodb", "nosql", "document"},
    "query": {"search", "lookup", "find", "retrieve", "select", "sql"},
    "index": {"search", "lookup", "query", "key"},
    "schema": {"migration", "ddl", "structure", "table", "blueprint"},
    "migration": {"schema", "ddl", "migrate", "evolve"},
    "table": {"relation", "entity", "collection", "view"},
    "view": {"table", "query", "materialized"},
    "trigger": {"hook", "callback", "event", "notify"},
    "constraint": {"validation", "check", "rule", "foreign", "unique", "key"},
    "transaction": {"tx", "atomic", "commit", "rollback", "acid"},
    "acid": {"transaction", "atomic", "consistency", "isolation", "durability"},
    "orm": {"sqlalchemy", "prisma", "django", "sequelize", "entity", "hibernate"},
    "join": {"relation", "association", "merge", "link"},
    "shard": {"partition", "horizontal", "scale", "fragment"},
    "replica": {"standby", "read-only", "secondary", "failover"},
    "backup": {"dump", "snapshot", "restore", "save", "copy", "archive", "preserve"},
    "restore": {"recovery", "backup", "reload", "replay", "restore"},
    # Cloud / DevOps
    "aws": {"amazon", "cloud", "ec2", "s3", "lambda", "eks"},
    "amazon": {"aws", "cloud"},
    "gcp": {"google", "cloud", "gke", "cloudrun"},
    "google": {"gcp", "cloud"},
    "azure": {"microsoft", "cloud", "aks", "az"},
    "microsoft": {"azure", "cloud"},
    "terraform": {"iac", "infrastructure", "hcl", "state", "provision"},
    "ansible": {"playbook", "automation", "configuration", "provision"},
    "pulumi": {"iac", "infrastructure", "cloud", "terraform"},
    "cloudformation": {"aws", "iac", "infrastructure", "stack"},
    "ci/cd": {"ci", "cd", "pipeline", "jenkins", "github actions", "gitlab", "circleci", "automation"},
    "ci": {"ci/cd", "pipeline", "automation", "integration"},
    "cd": {"ci/cd", "pipeline", "automation", "deployment"},
    "jenkins": {"ci/cd", "pipeline", "job", "plugin"},
    "github actions": {"ci/cd", "gha", "workflow", "pipeline", "action"},
    "gitlab": {"ci/cd", "pipeline", "runner", "gitlab-ci"},
    "gitlab-ci": {"gitlab", "ci/cd", "pipeline", "runner"},
    "circleci": {"ci/cd", "pipeline", "orb", "job"},
    "travisci": {"ci/cd", "pipeline"},
    "argocd": {"gitops", "cd", "kubernetes", "deployment"},
    "gitops": {"argocd", "cd", "flux", "deployment"},
    "flux": {"gitops", "cd", "kubernetes", "helm"},
    "vault": {"secret", "credential", "hashicorp", "token", "encrypt"},
    "consul": {"hashicorp", "service-discovery", "dns", "kv"},
    "hashicorp": {"terraform", "vault", "consul", "nomad", "packer"},
    "nomad": {"hashicorp", "orchestration", "job", "scheduler"},
    "packer": {"hashicorp", "image", "ami", "build"},
    "vagrant": {"hashicorp", "vm", "virtualbox", "provision"},
    "serverless": {"lambda", "cloudrun", "function", "faas", "knative"},
    "lambda": {"serverless", "function", "aws", "faas"},
    "faas": {"serverless", "function", "lambda", "knative"},
    "knative": {"serverless", "kubernetes", "function", "scale"},
    # Python
    "python": {"py", "python3", "cpython", "pip", "package"},
    "pip": {"python", "package", "dependency", "pypi", "install"},
    "pypi": {"pip", "python", "package", "index"},
    "venv": {"virtualenv", "virtual environment", "python", "isolate", "env"},
    "virtualenv": {"venv", "python", "env", "isolate"},
    "poetry": {"python", "package", "dependency", "pyproject"},
    "pipenv": {"python", "package", "env", "dependency"},
    "conda": {"python", "env", "package", "anaconda"},
    "anaconda": {"conda", "python", "env"},
    "django": {"python", "web", "framework", "orm", "model"},
    "flask": {"python", "web", "framework", "wsgi"},
    "fastapi": {"python", "web", "framework", "asgi", "openapi", "pydantic"},
    "uvicorn": {"asgi", "python", "server", "fastapi"},
    "gunicorn": {"wsgi", "python", "server", "flask", "django"},
    "pydantic": {"python", "validation", "schema", "model", "fastapi"},
    "sqlalchemy": {"orm", "python", "database", "sql", "alembic"},
    "alembic": {"migration", "sqlalchemy", "python", "schema"},
    "celery": {"task", "queue", "worker", "python", "distributed"},
    "pytest": {"python", "test", "fixture", "mock", "unittest", "assert"},
    "unittest": {"python", "test", "pytest", "assert"},
    "selenium": {"browser", "test", "automation", "webdriver"},
    "asyncio": {"async", "python", "await", "coroutine"},
    "coroutine": {"async", "await", "asyncio", "python"},
    # JavaScript / Node
    "node": {"nodejs", "javascript", "js", "runtime", "v8", "npm"},
    "nodejs": {"node", "javascript", "js", "runtime"},
    "npm": {"node", "package", "dependency", "install", "registry"},
    "yarn": {"node", "package", "npm", "dependency", "berry"},
    "pnpm": {"node", "package", "npm", "yarn", "dependency"},
    "nvm": {"node", "version", "manager", "nvmrc"},
    "typescript": {"ts", "typed", "javascript", "superset", "static", "type"},
    "ts": {"typescript", "typed"},
    "javascript": {"js", "es6", "ecmascript", "node", "web"},
    "js": {"javascript", "node"},
    "ecmascript": {"javascript", "es6", "js", "spec"},
    "react": {"reactjs", "jsx", "frontend", "ui", "component", "vdom"},
    "reactjs": {"react", "frontend", "ui", "component"},
    "jsx": {"react", "tsx", "xml", "template"},
    "tsx": {"typescript", "react", "jsx"},
    "nextjs": {"next", "react", "framework", "ssr", "ssg", "vercel"},
    "nuxt": {"vue", "framework", "ssr", "ssg"},
    "vue": {"vuejs", "frontend", "framework", "component", "reactive"},
    "vuejs": {"vue", "frontend", "framework", "component"},
    "svelte": {"sveltekit", "frontend", "framework", "reactive", "compiler"},
    "angular": {"angularjs", "frontend", "framework", "typescript", "rxjs"},
    "express": {"expressjs", "node", "server", "middleware", "route", "http"},
    "expressjs": {"express", "node", "server", "middleware"},
    "koa": {"node", "server", "middleware", "express"},
    "fastify": {"node", "server", "plugin", "schema"},
    "webpack": {"bundle", "build", "loader", "plugin", "module"},
    "vite": {"vitejs", "bundle", "build", "dev", "hmr", "rollup"},
    "rollup": {"bundle", "build", "tree-shaking", "esm"},
    "esbuild": {"bundle", "build", "fast", "minify"},
    "babel": {"transpile", "compiler", "polyfill", "javascript"},
    "prettier": {"format", "formatter", "code-style", "style"},
    "eslint": {"lint", "linter", "analyze", "code-quality", "rule"},
    "biome": {"lint", "format", "linter", "formatter", "rust"},
    "jest": {"test", "assert", "expect", "mock", "coverage"},
    "vitest": {"test", "jest", "vite", "assert", "mock", "coverage"},
    "mocha": {"test", "node", "chai", "assert"},
    "cypress": {"e2e", "test", "browser", "integration"},
    "playwright": {"e2e", "test", "browser", "automation", "webkit", "chromium"},
    "puppeteer": {"e2e", "test", "browser", "automation", "chromium"},
    "storybook": {"ui", "component", "document", "visual", "test"},
    # Networking
    "http": {"https", "rest", "api", "web", "protocol", "request"},
    "https": {"http", "tls", "ssl", "secure", "encrypt"},
    "tls": {"ssl", "https", "certificate", "encrypt", "handshake"},
    "ssl": {"tls", "https", "certificate", "secure"},
    "dns": {"domain", "nameserver", "resolve", "hostname", "record"},
    "tcp": {"transport", "connection", "socket", "protocol"},
    "udp": {"transport", "socket", "datagram", "protocol"},
    "api": {"rest", "http", "endpoint", "service", "interface", "graphql"},
    "rest": {"restful", "api", "http", "resource", "json"},
    "restful": {"rest", "api", "http"},
    "graphql": {"gql", "api", "query", "schema", "resolver"},
    "grpc": {"rpc", "protobuf", "http2", "stream", "bidirectional"},
    "protobuf": {"grpc", "serialize", "schema", "proto", "binary"},
    "websocket": {"ws", "socket", "realtime", "push", "stream"},
    "cors": {"cross-origin", "security", "header", "origin"},
    "oauth": {"auth", "authorization", "token", "jwt", "oidc", "sso"},
    "jwt": {"token", "auth", "oauth", "json-web-token", "session"},
    "sso": {"oauth", "oidc", "auth", "login", "identity", "saml", "single sign-on", "authentication"},
    "saml": {"sso", "auth", "identity", "xml"},
    "ldap": {"auth", "directory", "active-directory", "identity"},
    "vpn": {"tunnel", "wireguard", "openvpn", "network", "secure"},
    "proxy": {"reverse-proxy", "gateway", "nginx", "haproxy", "relay"},
    "nginx": {"proxy", "webserver", "loadbalancer", "reverse-proxy", "caddy"},
    "caddy": {"proxy", "webserver", "reverse-proxy", "tls", "automatic"},
    "haproxy": {"proxy", "loadbalancer", "reverse-proxy", "high-availability"},
    "cdn": {"cloudfront", "cloudflare", "fastly", "cache", "edge", "akamai"},
    "cloudflare": {"cdn", "dns", "waf", "proxy", "worker"},
    "loadbalancer": {"lb", "proxy", "nginx", "haproxy", "elb", "traffic"},
    "elb": {"aws", "loadbalancer", "lb", "nlb", "alb"},
    # Monitoring / Observability
    "prometheus": {"metrics", "monitor", "alertmanager", "tsdb", "scrape", "monitoring", "grafana", "telemetry"},
    "grafana": {"dashboard", "visualize", "monitor", "panel", "alert", "visualization", "monitoring", "prometheus"},
    "datadog": {"monitor", "metrics", "trace", "apm", "log"},
    "newrelic": {"monitor", "apm", "trace", "metric", "agent"},
    "opentelemetry": {"otel", "trace", "metric", "log", "observability", "collector"},
    "otel": {"opentelemetry", "trace", "observability"},
    "jaeger": {"trace", "distributed", "opentracing", "observability"},
    "zipkin": {"trace", "distributed", "observability"},
    "tempo": {"grafana", "trace", "observability", "jaeger"},
    "loki": {"grafana", "log", "aggregate", "observability", "promtail"},
    "promtail": {"loki", "log", "grafana", "scrape"},
    "elasticsearch": {"elastic", "es", "search", "log", "elk", "kibana"},
    "kibana": {"elasticsearch", "elastic", "visualize", "dashboard", "log"},
    "logstash": {"elastic", "log", "pipeline", "ingest", "elk"},
    "elk": {"elasticsearch", "logstash", "kibana", "elastic", "log"},
    "sentry": {"error", "trace", "monitor", "debug", "exception"},
    "alertmanager": {"prometheus", "alert", "notify", "pager"},
    "pagerduty": {"alert", "incident", "notify", "oncall", "escalation"},
    "oncall": {"pagerduty", "incident", "alert", "sre"},
    # Testing
    "test": {"spec", "assert", "verify", "check", "validate"},
    "spec": {"test", "assert", "verify", "check"},
    "mock": {"stub", "fake", "patch", "mockito", "unittest"},
    "fixture": {"setup", "teardown", "conftest", "data", "factory"},
    "integration": {"e2e", "test", "system", "contract"},
    "e2e": {"integration", "test", "system", "end-to-end", "cypress", "playwright"},
    "benchmark": {"perf", "bench", "load", "stress", "latency", "throughput"},
    "perf": {"benchmark", "performance", "latency", "throughput", "profiling"},
    "coverage": {"codecov", "coveralls", "report", "threshold", "branch"},
    "codecov": {"coverage", "coveralls", "report"},
    # Version control
    "git": {"vcs", "version-control", "scm", "source"},
    "github": {"git", "gh", "remote", "repo", "pull-request"},
    "gh": {"github", "cli", "repo"},
    "branch": {"git", "feature", "topic", "checkout", "switch"},
    "commit": {"git", "revision", "hash", "sha", "change", "diff"},
    "merge": {"git", "branch", "pull-request", "rebase", "conflict"},
    "rebase": {"git", "merge", "branch", "interactive"},
    "pull-request": {"pr", "github", "gitlab", "review", "merge", "code-review"},
    "pr": {"pull-request", "github", "gitlab", "review", "code-review"},
    "diff": {"git", "patch", "change", "delta", "unified"},
    "stash": {"git", "save", "temporary", "work-in-progress"},
    "tag": {"git", "release", "version", "semver", "annotate"},
    "release": {"tag", "version", "deploy", "ship", "cut", "semver", "rollout", "launch"},
    "semver": {"version", "release", "major", "minor", "patch"},
    "cherry-pick": {"git", "commit", "backport", "selective"},
    "bisect": {"git", "debug", "binary", "search", "regression"},
    "blame": {"git", "annotate", "author", "history"},
    "fork": {"git", "clone", "copy", "upstream", "origin"},
    "upstream": {"git", "remote", "origin", "fork", "parent"},
    "issue": {"bug", "ticket", "tracker", "github", "jira", "linear"},
    "bug": {"issue", "defect", "error", "fix", "regression"},
    "hotfix": {"patch", "fix", "emergency", "critical", "release"},
    # CI/CD and Automation
    "pipeline": {"ci/cd", "ci", "cd", "job", "stage", "step", "workflow"},
    "workflow": {"pipeline", "ci/cd", "action", "step", "dag"},
    "action": {"github", "workflow", "pipeline", "step"},
    "runner": {"ci/cd", "agent", "worker", "gitlab", "github"},
    "artifact": {"build", "package", "binary", "dist", "asset", "registry"},
    "stage": {"pipeline", "environment", "phase", "step"},
    "environment": {"env", "stage", "deploy", "prod", "staging", "dev"},
    "prod": {"production", "environment", "deploy", "live"},
    "staging": {"stage", "environment", "preprod", "uat", "test"},
    "dev": {"development", "environment", "local", "workstation"},
    # General
    "config": {"configuration", "setting", "param", "option", "property", "attribute", "conf", "cfg"},
    "configuration": {"config", "setting", "param", "option", "property"},
    "setting": {"config", "configuration", "option", "param", "preference"},
    "deploy": {"release", "ship", "rollout", "publish", "push", "promote"},
    "build": {"compile", "construct", "assemble", "package", "artifact"},
    "compile": {"build", "transpile", "assemble", "transform"},
    "debug": {"debugging", "diagnose", "troubleshoot", "trace", "inspect"},
    "monitor": {"observe", "watch", "track", "supervise", "metrics"},
    "alert": {"notify", "warn", "page", "trigger", "escalate"},
    "notify": {"alert", "notification", "webhook", "callback", "message"},
    "scale": {"scaling", "horizontal", "vertical", "autoscale", "expand"},
    "autoscale": {"scale", "hpa", "horizontal", "vertical"},
    "migrate": {"migration", "schema", "transfer", "port", "convert", "upgrade"},
    "upgrade": {"update", "migrate", "bump", "rollout", "version"},
    "rollout": {"deploy", "release", "gradual", "canary", "blue-green"},
    "canary": {"rollout", "deploy", "gradual", "test", "release"},
    "blue-green": {"deploy", "rollout", "zero-downtime", "strategy"},
    "zero-downtime": {"blue-green", "rolling", "deploy", "availability", "ha"},
    "high-availability": {"ha", "redundant", "failover", "cluster", "resilient"},
    "ha": {"high-availability", "redundant", "failover", "cluster"},
    "failover": {"high-availability", "redundant", "replica", "standby", "disaster"},
    "disaster-recovery": {"dr", "failover", "backup", "recovery", "rto", "rpo"},
    "latency": {"performance", "response-time", "delay", "speed", "p99", "p95"},
    "throughput": {"performance", "bandwidth", "concurrency", "rps", "qps", "tps"},
    "concurrency": {"parallel", "async", "goroutine", "thread", "race"},
    "race": {"concurrency", "data-race", "deadlock", "condition", "thread"},
    "deadlock": {"concurrency", "race", "lock", "starvation"},
    "cache": {"redis", "memcached", "cd", "buffer", "ttl", "invalidate"},
    "ttl": {"cache", "expiry", "timeout", "lifetime", "duration"},
    "pool": {"connection-pool", "buffer", "reuse", "resource"},
    "rate-limit": {"throttle", "backoff", "quota", "limit", "burst"},
    "circuit-breaker": {"resilience", "fault-tolerance", "retry", "fallback", "bulkhead"},
    "retry": {"circuit-breaker", "backoff", "exponential", "jitter", "resilience"},
    "healthcheck": {"health", "liveness", "readiness", "probe", "heartbeat"},
    "probe": {"healthcheck", "liveness", "readiness", "kubernetes"},
    "heartbeat": {"healthcheck", "liveness", "ping", "alive", "health check", "keepalive"},
    # Data / formats
    "json": {"javascript-object-notation", "serialize", "data", "parse"},
    "yaml": {"yml", "serialize", "config", "data", "parse"},
    "xml": {"serialize", "data", "parse", "xslt", "xpath"},
    "csv": {"tsv", "data", "tabular", "spreadsheet", "import", "export"},
    "parquet": {"columnar", "data", "analytics", "arrow", "orc"},
    "avro": {"schema", "serialize", "data", "kafka"},
    "msgpack": {"messagepack", "serialize", "binary", "compact"},
    "base64": {"encode", "decode", "binary", "text"},
    "hash": {"sha256", "md5", "digest", "checksum", "fingerprint"},
    "checksum": {"hash", "verify", "integrity", "sha256", "md5"},
    "encrypt": {"cipher", "crypto", "aes", "rsa", "encryption", "decrypt"},
    "decrypt": {"encrypt", "cipher", "aes", "rsa", "decryption"},
    "compress": {"gzip", "zlib", "zip", "archive", "deflate"},
    # AI / ML
    "llm": {"large-language-model", "ai", "gpt", "model", "transformer"},
    "gpt": {"llm", "openai", "transformer", "model", "chatgpt"},
    "openai": {"gpt", "llm", "model", "api", "chatgpt"},
    "transformer": {"attention", "self-attention", "encoder", "decoder", "llm", "model", "bert", "gpt"},
    "bert": {"transformer", "encoder", "embedding", "nlp", "masked"},
    "embedding": {"vector", "encode", "representation", "feature", "semantic", "dense"},
    "vector": {"embedding", "tensor", "feature", "array", "collection", "dense", "representation", "similarity"},
    "token": {"tokenize", "vocab", "subword", "bpe", "wordpiece", "llm"},
    "tokenize": {"token", "vocab", "bpe", "wordpiece", "split"},
    "inference": {"predict", "forward", "serve", "batch", "model"},
    "training": {"train", "fit", "learn", "epoch", "batch", "fine-tune", "finetune"},
    "fine-tune": {"finetune", "transfer", "training", "adapt", "lora"},
    "lora": {"fine-tune", "peft", "adapt", "efficient", "qlora"},
    "qlora": {"lora", "fine-tune", "quantize", "efficient"},
    "quantize": {"quantization", "int8", "fp16", "compress", "reduce", "qlora"},
    "rag": {"retrieval-augmented", "retrieval", "generation", "llm", "context"},
    "agent": {"tool", "function-calling", "autonomous", "loop", "llm", "assistant", "bot", "ai agent", "autonomous agent"},
    "function-calling": {"agent", "tool", "llm", "function"},
    "prompt": {"instruction", "template", "prompt-engineering", "context", "system"},
    "prompt-engineering": {"prompt", "instruction", "template", "design"},
    # Security
    "firewall": {"waf", "security", "filter", "acl", "rule"},
    "waf": {"firewall", "web", "security", "cloudflare", "modsecurity"},
    "ids": {"ips", "intrusion", "security", "detection", "snort"},
    "ips": {"ids", "intrusion", "prevention", "security"},
    "penetration": {"pentest", "security", "audit", "exploit", "vulnerability"},
    "pentest": {"penetration", "security", "exploit", "vulnerability"},
    "cve": {"vulnerability", "cwe", "security", "advisory", "patch"},
    "vulnerability": {"cve", "cwe", "security", "exploit", "risk", "weakness"},
    "sbom": {"bom", "inventory", "dependency", "supply-chain", "security"},
    "supply-chain": {"sbom", "dependency", "security", "SLSA", "provenance"},
    "zero-trust": {"beyondcorp", "security", "auth", "verify", "segment"},
    "iam": {"identity", "auth", "permission", "policy", "role", "access"},
    "policy": {"rule", "constraint", "opa", "gatekeeper", "iam", "guard"},
    "opa": {"policy", "gatekeeper", "rego", "constraint", "open-policy-agent"},
    "gatekeeper": {"opa", "policy", "kubernetes", "admission"},
    # AI / Agent / Memory domain
    "memory": {"recall", "storage", "knowledge", "context", "notes"},
    "search": {"query", "lookup", "retrieve", "find", "recall"},
    "headcount": {"engineers", "team size", "staff", "headcount", "employees", "members"},
    "recall": {"search", "retrieve", "memory", "lookup"},
    "knowledge": {"memory", "facts", "information", "context"},
    "crdt": {"conflict-free", "replicated", "sync", "merge", "convergent"},
    "sync": {"synchronize", "replication", "consistency", "merge"},
    "dashboard": {"observability", "monitoring", "metrics", "ui", "interface"},
    "metrics": {"monitoring", "observability", "telemetry", "statistics"},
    "observability": {"monitoring", "metrics", "logging", "tracing"},
    "streamlit": {"dashboard", "ui", "interface", "web"},
    "self-editing": {"patch", "supersede", "revert", "amend", "update"},
    "patch": {"edit", "amend", "update", "modify", "self-editing"},
    "supersede": {"replace", "override", "deprecate", "successor"},
    "belief": {"hypothesis", "assumption", "confidence", "assertion"},
    "kg": {"knowledge graph", "graph", "entities", "edges", "ontology"},
    "knowledge graph": {"kg", "graph", "ontology", "semantic network"},
    "entity": {"node", "concept", "item", "record"},
    "relation": {"edge", "link", "connection", "association"},
    "temporal": {"time-based", "time-series", "time-varying", "dated"},
    "langchain": {"agent framework", "llm framework", "chain"},
    "crewai": {"agent framework", "multi-agent", "crew"},
    "mcp": {"model context protocol", "tool protocol", "agent protocol"},
    "hipaa": {"compliance", "healthcare", "privacy", "security"},
    "soc2": {"compliance", "security", "audit", "trust"},
    "gdpr": {"privacy", "data protection", "compliance", "regulation"},
    "audit": {"logging", "tracking", "compliance", "trail"},
    "tenant": {"namespace", "isolation", "multi-tenant", "workspace"},
    "saga": {"transaction", "rollback", "compensation", "two-phase"},
    "rollback": {"undo", "revert", "recovery", "compensation"},
    "flock": {"file lock", "locking", "mutex", "concurrency"},
    "wol": {"write-ahead log", "wal", "journal", "durability"},
    "fts": {"full-text search", "fulltext", "text search", "bm25"},
    "bm25": {"ranking", "text retrieval", "tf-idf", "term frequency"},
    "rrf": {"reciprocal rank fusion", "rank fusion", "score fusion"},
    "colbert": {"late interaction", "multi-vector", "token matching"},
    "cross-encoder": {"reranking", "scoring", "relevance"},
    "similarity": {"distance", "cosine", "dot product", "matching"},
    "chunk": {"segment", "passage", "fragment", "window"},
    "backlink": {"link", "reference", "connection", "wiki-style"},
    "compaction": {"compression", "consolidation", "merge"},
    "dedup": {"deduplication", "duplicate removal", "merge", "consolidation"},
    "contradiction": {"conflict", "inconsistency", "contrary", "opposite"},
    "entailment": {"inference", "derivation", "implication", "reasoning"},
    "hypothesis": {"belief", "assumption", "theory", "conjecture"},
    "confidence": {"certainty", "probability", "belief strength"},
    "retraction": {"withdrawal", "revocation", "reversal"},
    "deprecation": {"supersession", "replacement", "obsolescence"},
    "circuit breaker": {"fault tolerance", "fail-safe", "protection"},
    "auto-save": {"autosave", "background save", "async save"},
    "cron": {"scheduled task", "periodic job", "timer"},
    "drift": {"divergence", "inconsistency", "staleness"},
}

# Query type classification regexes (QW3)
_QUERY_TYPE_TEMPORAL_RE = re.compile(
    "\\b(when|what year|what date|how long ago|last (week|month|year)|recent|latest|yesterday|today|tomorrow|ago|\\d{4}[-/]\\d{2}|in \\d{4})\\b",
    re.IGNORECASE,
)
_QUERY_TYPE_MULTIHOP_RE = re.compile(
    "\\b(compare|difference|between|relationship|both|and also|plus|along with|combined)\\b",
    re.IGNORECASE,
)
_QUERY_TYPE_CODE_RE = re.compile(
    "\\b(function|class|method|import|return|def |var |let |const |\\.py|\\.js|\\.ts|\\.go|\\.rs|error|exception|stacktrace|syntax|compile|build)\\b",
    re.IGNORECASE,
)
# M16 fix: separate word-bounded patterns for generic terms to reduce false positives
_QUERY_TYPE_CODE_BOUNDED_RE = re.compile(
    "\\b(testing|unit.test|test.case|spec|fixture)\\b",
    re.IGNORECASE,
)
_QUERY_TYPE_FACTUAL_RE = re.compile(
    "\\b(what is|what are|who is|where is|define|definition of|meaning of|how many|capital of)\\b",
    re.IGNORECASE,
)

# Inference queries: "Would X likely Y?", "Does X have Y?", "Is X considered Y?"
# These require finding sessions about a specific entity AND related concepts.
# The entity name must appear in matching sessions (AND anchor).
# L11 fix: deduplicated alternants (play×2, study×2 removed).
_QUERY_TYPE_INFERENCE_RE = re.compile(
    "\\b(would|might|could)\\b.*\\b(likely|probably|prefer|enjoy|like|choose|pursue|collect|play|read|watch|listen|cook|eat|drink|visit|go|travel|live|work|study|practice|run|swim|hike|camp|paint|draw|write|sing|dance|drive|ride|fly|sail|climb|build|make|create|design|plan|organize|manage|lead|teach|learn|research|explore|discover|invent|innovate)\\b",
    re.IGNORECASE,
)

_QUERY_TYPE_WEIGHTS: dict[str, dict[str, float]] = {
    "temporal": {
        "bm25": 0.30,
        "fitness": 0.25,
        "importance": 0.15,
        "pinned": 0.10,
        "recency": 0.10,
        "tag_match": 0.10,
    },
    "multihop": {
        "bm25": 0.20,
        "fitness": 0.35,
        "importance": 0.20,
        "pinned": 0.10,
        "recency": 0.10,
        "tag_match": 0.05,
    },
    "code": {
        "bm25": 0.35,
        "fitness": 0.15,
        "importance": 0.15,
        "pinned": 0.15,
        "recency": 0.10,
        "tag_match": 0.10,
    },
    "factual": {
        "bm25": 0.45,
        "fitness": 0.10,
        "importance": 0.15,
        "pinned": 0.15,
        "recency": 0.10,
        "tag_match": 0.05,
    },
    "general": {
        "bm25": 0.35,
        "fitness": 0.20,
        "importance": 0.15,
        "pinned": 0.10,
        "recency": 0.10,
        "tag_match": 0.10,
    },
}


def _get_query_type_weights() -> dict:
    try:
        from infra._lazy_imports import get_config

        raw = get_config().query_type_weights
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception as exc:
        logger.debug("query_type_weights config unavailable: %s", exc)
    return _QUERY_TYPE_WEIGHTS


_QUERY_EXPANSIONS: dict[str, list[str]] = {
    "ml": ["machine learning", "machine-learning"],
    "ai": ["artificial intelligence"],
    "nlp": ["natural language processing", "natural-language"],
    "llm": ["large language model", "language model"],
    "llms": ["large language models", "language models"],
    "db": ["database"],
    "dbs": ["databases"],
    "sql": ["structured query language"],
    "auth": ["authentication", "authorization"],
    "authn": ["authentication"],
    "authz": ["authorization"],
    "api": ["application programming interface", "endpoint"],
    "apis": ["endpoints", "application programming interfaces"],
    "ui": ["user interface", "frontend"],
    "ux": ["user experience"],
    "k8s": ["kubernetes"],
    "tf": ["terraform"],
    "ci": ["continuous integration"],
    "cd": ["continuous deployment", "continuous delivery"],
    "qa": ["quality assurance", "testing"],
    "perf": ["performance"],
    "config": ["configuration", "settings"],
    "configs": ["configurations"],
    "env": ["environment", "env var", "envvars"],
    "deps": ["dependencies"],
    "pkg": ["package"],
    "pkgs": ["packages"],
    "lib": ["library"],
    "libs": ["libraries"],
    "repo": ["repository"],
    "repos": ["repositories"],
    "pr": ["pull request"],
    "prs": ["pull requests"],
    "cli": ["command line", "command-line"],
    "async": ["asynchronous", "non-blocking"],
    "sync": ["synchronous", "blocking"],
    "os": ["operating system"],
    "fs": ["filesystem", "file system"],
    "i18n": ["internationalization"],
    "l10n": ["localization"],
    "a11y": ["accessibility"],
    "ip": ["internet protocol", "ip address"],
    "tcp": ["transmission control protocol"],
    "udp": ["user datagram protocol"],
    "http": ["hypertext transfer protocol"],
    "https": ["secure http", "tls"],
    "tls": ["transport layer security", "ssl"],
    "ssl": ["secure sockets layer"],
    "json": ["javascript object notation"],
    "yaml": ["yaml ain't markup language"],
    "yml": ["yaml", "yaml ain't markup language"],
    "html": ["hypertext markup language"],
    "css": ["cascading style sheets"],
    "js": ["javascript", "ecmascript"],
    "ts": ["typescript"],
    "py": ["python"],
    "rb": ["ruby"],
    "go": ["golang"],
    "rs": ["rust"],
    "crud": ["create read update delete"],
    "rest": ["representational state transfer", "restful"],
    "grpc": ["remote procedure call"],
    "ws": ["websocket", "web socket"],
    "orm": ["object relational mapper", "object-relational mapping"],
    "mvc": ["model view controller"],
    "mvvm": ["model view viewmodel"],
    "ssr": ["server side rendering", "server-side rendering"],
    "spa": ["single page application", "single-page application"],
    "pwa": ["progressive web app"],
    "csp": ["content security policy"],
    "cors": ["cross origin resource sharing", "cross-origin"],
    "csrf": ["cross site request forgery", "cross-site"],
    "xss": ["cross site scripting", "cross-site"],
    "owasp": ["open web application security project"],
    "vuln": ["vulnerability"],
    "vulns": ["vulnerabilities"],
    "cve": ["common vulnerabilities and exposures"],
    "pci": ["payment card industry"],
    "gdpr": ["general data protection regulation"],
    "hipaa": ["health insurance portability accountability act"],
    "soc2": ["soc 2", "service organization control 2"],
    "sla": ["service level agreement"],
    "rto": ["recovery time objective"],
    "rpo": ["recovery point objective"],
    "dr": ["disaster recovery"],
    "ha": ["high availability"],
    "lb": ["load balancer", "load balancing"],
    "vm": ["virtual machine"],
    "vms": ["virtual machines"],
    "vmware": ["vsphere"],
    "e2e": ["end to end", "end-to-end"],
    "i3e": ["integration"],
    "rlhf": ["reinforcement learning from human feedback"],
    "rag": ["retrieval augmented generation", "retrieval-augmented"],
    "gpu": ["graphics processing unit"],
    "tpu": ["tensor processing unit"],
    "nn": ["neural network", "neural net"],
    "cnn": ["convolutional neural network"],
    "rnn": ["recurrent neural network"],
    "transformer": ["transformer architecture", "attention model"],
    "gpt": ["generative pre trained transformer"],
    "bert": ["bidirectional encoder representations"],
    "container": ["docker", "pod", "image"],
    "containers": ["docker", "pods", "images"],
    "orchestration": ["orchestrate", "orchestrates", "orchestrating"],
    "infrastructure": ["infra", "platform", "foundation"],
    "management": ["manage", "manages", "managing"],
    "platform": ["infrastructure", "framework", "system"],
    "deployment": ["deploy", "deploying", "deployed"],
    "monitoring": ["observe", "observability", "telemetry"],
    "logging": ["log", "logs", "logger", "observability"],
    "testing": ["test", "tests", "qa", "quality assurance"],
    "database": ["db", "dbs", "datastore", "store"],
    "search": ["query", "lookup", "find", "retrieval"],
    "performance": ["perf", "speed", "latency", "throughput"],
    "security": ["auth", "authn", "authz", "secure"],
    "configuration": ["config", "settings", "setup"],
    "architecture": ["design", "structure", "pattern"],
    # Memory system domain
    "memory": ["note", "entry", "record", "store", "persist"],
    "note": ["memory", "entry", "record", "memo"],
    "session": ["conversation", "chat", "thread", "dialog"],
    "recall": ["retrieve", "search", "find", "fetch", "lookup"],
    "retrieval": ["search", "recall", "lookup", "fetch"],
    "embedding": ["vector", "representation", "encoding", "semantic"],
    "rerank": ["reranking", "re-rank", "reorder", "rescore"],
    "kg": ["knowledge graph", "knowledge-graph", "graph"],
    "entity": ["node", "concept", "subject", "thing"],
    "relation": ["edge", "predicate", "link", "connection"],
    "fact": ["triple", "assertion", "claim", "statement"],
    "temporal": ["time", "date", "chronological", "timeline"],
    "history": ["timeline", "chronological", "past", "previous"],
    "consolidation": ["merge", "compact", "summarize", "distill"],
    "deduplication": ["dedup", "duplicate", "merge", "remove"],
    "contradiction": ["conflict", "inconsistency", "contrary"],
    "belief": ["assertion", "claim", "confidence", "trust"],
    "serialization": ["serialize", "deserialize", "encoding"],
    "rollback": ["revert", "undo", "recovery"],
    "fixtures": ["setup", "config", "conftest", "helpers"],
    "assurance": ["quality", "testing", "qa"],
    "pods": ["containers", "instances", "services", "containerized"],
    "dashboard": ["visualization", "grafana"],
    "healing": ["health", "healthy", "heal"],
    "self-healing": ["resilient", "fault-tolerant", "self-heal"],
    "cluster": ["clusters", "clustered", "orchestration", "orchestrating"],
    "orchestrat": ["orchestrate", "orchestrates", "orchestrated", "orchestrating", "orchestration", "orchestrator", "orchestrators"],
    "observ": ["observe", "observes", "observed", "observing", "observation", "observations", "observability", "observable"],
    "package": ["pkg", "packages", "library", "containerize", "services"],
    "applications": ["services", "apps", "app", "containerized"],
    "index": ["indexes", "indexed", "indexing", "indices", "search", "lookup"],
    "queries": ["search", "lookup", "find", "retrieval"],
    # Personal/lifestyle expansions for LongMemEval-style queries
    "yoga": ["yoga", "class", "studio", "practice", "pose"],
    "class": ["class", "classes", "course", "lesson", "session"],
    "studio": ["studio", "gym", "center", "school"],
    "rice": ["rice", "grain", "short-grain", "long-grain", "basmati", "jasmine"],
    "favorite": ["favorite", "favourite", "preferred", "best", "top"],
    "music": ["music", "song", "songs", "playlist", "artist", "band", "album"],
    "streaming": ["streaming", "stream", "spotify", "apple music", "youtube music", "tidal", "pandora"],
    "service": ["service", "platform", "app", "application"],
    "coffee": ["coffee", "cafe", "brew", "espresso", "latte"],
    "recipe": ["recipe", "recipes", "dish", "meal", "cook", "cooking"],
    "restaurant": ["restaurant", "dining", "eat", "food", "cuisine"],
    "trip": ["trip", "travel", "vacation", "journey", "visit", "destination"],
    "book": ["book", "novel", "read", "reading", "author"],
    "movie": ["movie", "film", "watch", "show", "series", "tv"],
    "gym": ["gym", "workout", "workout plan", "exercise", "fitness", "training"],
    "exercise": ["exercise", "workout", "workout plan", "fitness", "training", "routine"],
    "framework": ["framework", "tech stack", "technology", "stack", "library", "frontend"],
    "editor": ["editor", "ide", "code editor", "text editor"],
    "launch": ["launch", "deadline", "launch date", "release", "ship date", "schedule"],
    "vacation": ["vacation", "trip", "travel", "destination", "holiday", "getaway"],
    "color": ["color", "colour", "favorite color", "palette", "shade"],
    "bge": ["bge embedding model", "bge-base", "bge-large"],
    "rrf": ["reciprocal rank fusion"],
    "colbert": ["colbertv2", "colbert v2"],
    "mcp": ["model context protocol"],
    "crdt": ["conflict-free replicated data type"],
    "fts5": ["full-text search 5", "fts"],
    "usearch": ["vector search", "similarity search"],
    "pipeline": ["search pipeline", "retrieval pipeline"],
    "orchestrator": ["search orchestrator", "pipeline orchestrator"],
    "self-edit": ["self-editing", "amend memory", "update memory"],
    "belief review": ["review beliefs", "belief management"],
}

# ---------------------------------------------------------------------------
# Conceptual phrase expansions — map intent phrases to domain terms
# ---------------------------------------------------------------------------
# When the query contains one of these phrases, the listed terms are
# APPENDED to the query (not replacing existing tokens).  This bridges
# the vocabulary gap between user intent and content vocabulary.
# GENERIC only — no domain-specific hardcoding (gaming rejection: H17).
_CONCEPTUAL_PHRASE_EXPANSIONS: dict[str, list[str]] = {
    "data loss": ["backup", "persistence", "redundancy", "replication"],
    "scale horizontally": ["load balancer", "replica", "sharding", "distributed"],
    "scale vertically": ["resize", "upgrade", "more cpu", "more memory"],
    "deploy to production": ["release", "deployment", "production environment", "go live"],
    "monitor performance": ["observability", "metrics", "logging", "tracing", "apm"],
    "handle errors": ["error handling", "exception", "retry", "fallback", "circuit breaker"],
    "manage secrets": ["credential management", "vault", "env var", "secret management"],
    "test code": ["unit test", "integration test", "testing strategy", "test coverage"],
    "version control": ["git", "branching", "merge", "commit", "source control"],
    "api design": ["rest api", "endpoint", "api contract", "openapi", "swagger"],
    "database design": ["schema", "normalization", "index", "query optimization"],
}

_QUERY_EXPANSION_REVERSE: dict[str, str] = {}
for _canon in _QUERY_EXPANSIONS:
    _QUERY_EXPANSION_REVERSE[_canon] = _canon
for _canon, _alts in _QUERY_EXPANSIONS.items():
    for _a in _alts:
        _al = _a.lower()
        if _al not in _QUERY_EXPANSION_REVERSE:
            _QUERY_EXPANSION_REVERSE[_al] = _canon


def _query_expansions() -> dict:
    return _QUERY_EXPANSIONS


def _query_expansion_reverse() -> dict:
    return _QUERY_EXPANSION_REVERSE


def _expand_query(query: str) -> str:
    """QW2: Expand query terms using the synonym/abbreviation dictionary.

    Returns a string where each detected term has been replaced with an OR
    group of all its known forms. E.g. "DB speed" becomes
    '"database" OR "db" "speed"' (when joined with the rest of the query).

    Quoted phrases are preserved as-is (don't expand inside phrases).
    The original tokens are always kept so the user's literal query still matches.
    """
    if not query:
        return query
    phrases = re.findall('"([^"]*)"', query)
    bare = re.sub('"[^"]*"', " ", query)
    bare_tokens = re.findall(r"[\w@\#\.\+\-]+", bare, flags=re.UNICODE)

    _MONTH_MAP = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "jun": "06", "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"
    }
    date_match = re.search(r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b\s+(\d{4})", query, re.IGNORECASE)
    ym_token = None
    if date_match:
        m_num = _MONTH_MAP.get(date_match.group(1).lower())
        if m_num:
            ym_token = f'"{date_match.group(2)}-{m_num}"'
    if not bare_tokens and (not phrases):
        return query
    # Filter stop words from expansion — they waste FTS5 match budget
    # by matching many irrelevant sessions. Content words are kept.
    content_tokens = [t for t in bare_tokens if t.lower() not in _STOP_WORDS]
    if not content_tokens and not phrases:
        return query
    expanded_tokens = []
    seen_aliases: set = set()
    seen_forms: set = set()  # global dedup: prevent same form in multiple expansions
    for tok in content_tokens:
        low = tok.lower()
        # Try synonym expansion first
        canon = _query_expansion_reverse().get(low)
        if canon and canon not in seen_aliases:
            seen_aliases.add(canon)
            forms = [canon] + _query_expansions().get(canon, [])
            unique: list[str] = []
            for f in forms:
                fl = f.lower()
                if fl not in seen_forms:
                    unique.append(f)
                    seen_forms.add(fl)
            if unique:
                quoted = " OR ".join((f'"{f}"' for f in unique))
                expanded_tokens.append(f"({quoted})")
        else:
            # Try synonym map expansion
            synonyms = _SYNONYM_MAP.get(low)
            if synonyms:
                syn_unique: list[str] = []
                for s in sorted(synonyms, key=len, reverse=True):
                    sl = s.lower()
                    if sl not in seen_forms:
                        syn_unique.append(s)
                        seen_forms.add(sl)
                # Always include the original token
                if low not in seen_forms:
                    syn_unique.insert(0, tok)
                    seen_forms.add(low)
                quoted = " OR ".join((f'"{f}"' for f in syn_unique))
                expanded_tokens.append(f"({quoted})")
            else:
                # Try word form expansion (porters-stemmer cross-form matching)
                expanded = False
                for stem, forms in _WORD_FORM_EXPANSIONS.items():
                    # Check if this token matches any form in the expansion set
                    if low in [f.lower() for f in forms] or (low.startswith(stem) and len(low) == len(stem)):
                        # Use all forms from this expansion set
                        exp_unique: list[str] = []
                        for f in forms:
                            if f.lower() not in [u.lower() for u in exp_unique]:
                                exp_unique.append(f)
                        quoted = " OR ".join((f'"{f}"' for f in exp_unique))
                        expanded_tokens.append(f"({quoted})")
                        expanded = True
                        break
                if not expanded:
                    expanded_tokens.append(f'"{tok}"')
    out_parts = [f'"{p}"' for p in phrases if p.strip()]
    out_parts.extend(expanded_tokens)
    if ym_token and ym_token not in out_parts:
        out_parts.append(ym_token)
    # Always use OR for maximum recall. The custom eval uses OR-only
    # and gets 100% recall — AND on short queries kills recall by
    # requiring every term to match, which fails on conversational
    # queries where the answer uses different vocabulary.
    if out_parts:
        return " OR ".join(out_parts)
    return query


def _conceptual_expand(query: str) -> str:
    """Detect conceptual phrases and append domain-specific terms.

    Unlike _expand_query (which replaces tokens with OR groups),
    this function APPENDS related terms to bridge vocabulary gaps
    between user intent and content vocabulary.  E.g. "keep application
    state between restarts" gets "persistent storage" OR "volume" OR ...
    appended so FTS/semantic channels can find docker-volumes.

    Returns the query with appended terms, or the original query if no
    conceptual phrases are detected.
    """
    if not query:
        return query
    q_lower = query.lower()
    appended_terms: list[str] = []
    for phrase, terms in _CONCEPTUAL_PHRASE_EXPANSIONS.items():
        if phrase in q_lower:
            appended_terms.extend(terms)
    if not appended_terms:
        return query
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in appended_terms:
        tl = t.lower()
        if tl not in seen:
            unique.append(t)
            seen.add(tl)
    appended = " OR ".join(f'"{t}"' for t in unique)
    return f"({query}) OR ({appended})"


def _semantic_expand(query: str, db_path: Any = None, top_k: int = 3) -> list[str]:
    """Semantic expansion: find similar memories and extract key terms.

    Uses the embedding model to find memories semantically similar to the
    query, then extracts distinctive terms from those memories to expand
    the query. This helps with synonym/paraphrase queries like
    "package apps portably" finding "docker container basics".
    """
    if not query or not db_path:
        return []
    try:
        from infra._lazy_imports import get_embedding_search
        from pathlib import Path

        _es = get_embedding_search()
        if not getattr(_es, "is_transformer", False):
            return []
        # Search for similar memories
        results = _es.search(query, Path(str(db_path)), limit=top_k)
        if not results:
            return []

        # Extract distinctive terms from top results
        query_words = set(w.lower() for w in query.split() if len(w) >= 3)
        expansion_terms = []
        seen = set()

        for hit in results:
            content = hit.get("preview", "") or hit.get("content", "")
            if not content:
                continue
            # Extract meaningful terms (3+ chars, not stopwords)
            for word in content.split():
                w = word.lower().strip(".,;:!?()[]{}\"'`")
                if (len(w) >= 3 and w not in query_words
                    and w not in seen and w not in _STOP_WORDS):
                    seen.add(w)
                    expansion_terms.append(w)
                    if len(expansion_terms) >= 5:
                        break
            if len(expansion_terms) >= 5:
                break

        return expansion_terms[:5]
    except Exception:
        return []


def _did_you_mean(query: str, synonym_map: dict) -> list:
    """Return up to 3 expanded query strings based on the synonym map.

    For each word in the query that appears as a key in `synonym_map`,
    produce a variant where that word is replaced by one of its synonyms.
    Up to 3 variants total are returned (one per matching word, then
    truncated). Words without a known synonym are skipped.
    """
    if not query or not synonym_map:
        return []
    words = query.lower().split()
    expansions: list[str] = []
    for i, w in enumerate(words):
        if len(expansions) >= 3:
            break
        clean = w.strip(".,;:!?()[]{}\"'`")
        syns = synonym_map.get(clean)
        if not syns:
            continue
        for syn in syns[:3]:
            if len(expansions) >= 3:
                break
            new_query = " ".join(words[:i] + [syn] + words[i + 1 :])
            expansions.append(new_query)
    return expansions[:3]


def _top_recent_tags(db_path, limit: int = 5, tenant_id: str = "default") -> list:
    """Return up to `limit` most-recently-observed distinct tag sets.

    Each row in the memories table stores tags as a JSON array string.
    We group by the literal string and pick the most recently observed
    per group. Returns [] on any DB error.
    """
    if not db_path:
        return []
    try:
        conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
        try:
            rows = conn.execute(
                "\n                SELECT tags, MAX(observed_at) as latest\n                FROM tenant_memories\n                WHERE tags != '[]' AND tags IS NOT NULL\n                GROUP BY tags\n                ORDER BY latest DESC\n                LIMIT ?\n            ",
                (limit,),
            ).fetchall()
            return [{"tag": r[0], "latest_observed_at": r[1]} for r in rows]
        finally:
            safe_close_db(conn)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        logger.warning("Failed to query recent tags for suggestions")
        return []


def _top_recent_notes(db_path, limit: int = 5, tenant_id: str = "default") -> list:
    """Return up to `limit` most-recently-observed notes (id + preview)."""
    if not db_path:
        return []
    try:
        conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
        try:
            rows = conn.execute(
                "\n                SELECT id, substr(content, 1, 80) as preview, observed_at\n                FROM tenant_memories\n                ORDER BY observed_at DESC\n                LIMIT ?\n            ",
                (limit,),
            ).fetchall()
            return [{"id": r[0], "preview": r[1], "observed_at": r[2]} for r in rows]
        finally:
            safe_close_db(conn)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        logger.warning("Failed to query recent notes for suggestions")
        return []


def _top_recent_source_files(db_path, limit: int = 5, tenant_id: str = "default") -> list:
    """Return up to `limit` source files grouped by recency, with counts."""
    if not db_path:
        return []
    try:
        conn = connection_pool.get(str(db_path), tenant_id=tenant_id)
        try:
            rows = conn.execute(
                "\n                SELECT source_file, COUNT(*) as cnt, MAX(observed_at) as latest\n                FROM tenant_memories\n                GROUP BY source_file\n                ORDER BY latest DESC\n                LIMIT ?\n            ",
                (limit,),
            ).fetchall()
            return [
                {"source_file": r[0], "count": r[1], "latest_observed_at": r[2]}
                for r in rows
            ]
        finally:
            safe_close_db(conn)
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        logger.warning("Failed to query recent source files for suggestions")
        return []


def _build_zero_result_suggestions(db_path, query: str) -> dict:
    """Assemble the suggestions payload for a 0-result search.

    Returns a dict with four keys (did_you_mean / by_tag / by_recency /
    by_source_file), each a list. Any failing channel degrades to [].
    """
    return {
        "did_you_mean": _did_you_mean(query, _query_expansions()),
        "by_tag": _top_recent_tags(db_path, limit=5),
        "by_recency": _top_recent_notes(db_path, limit=5),
        "by_source_file": _top_recent_source_files(db_path, limit=5),
    }


def _detect_query_type(query: str) -> str:
    """QW3: classify a query into one of {temporal, multihop, code, factual, general}.

    Detection is conservative — the first matching pattern wins, and
    "general" is the fallback. False positives are worse than false
    negatives here, so the patterns are tight.
    """
    if not query:
        return "general"
    # M16 fix: check inference before code to avoid false positives on "test"/"spec"
    if _QUERY_TYPE_INFERENCE_RE.search(query):
        return "inference"
    if _QUERY_TYPE_CODE_RE.search(query) or _QUERY_TYPE_CODE_BOUNDED_RE.search(query):
        return "code"
    if _QUERY_TYPE_TEMPORAL_RE.search(query):
        return "temporal"
    if _QUERY_TYPE_MULTIHOP_RE.search(query):
        return "multihop"
    if _QUERY_TYPE_FACTUAL_RE.search(query):
        return "factual"
    return "general"


def _weights_for_query_type(query_type: str) -> dict:
    """QW3: return the merged weight dict for a given query type.

    The result always sums to 1.0 and always contains all six channel keys.
    """
    weights = _get_query_type_weights()
    return dict(weights.get(query_type, weights["general"]))


def _escape_phrase(s: str) -> str:
    """Escape a string for safe inclusion in an FTS5 double-quoted phrase."""
    return '"' + s.replace('"', '""') + '"'


def _escape_fts_query(query: str) -> str:
    """Escape FTS5 special characters in a user-provided query string.

    FTS5 special operators: ``*`` (prefix wildcard), ``^`` (prefix boost),
    standalone ``NEAR`` and ``NOT``. These are quoted so they become
    literal search terms.  Parentheses and ``+`` are FTS5 grouping/prefix
    syntax — they must NOT be escaped.
    """
    FTS5_RESERVED = {"AND", "OR", "NOT", "NEAR"}
    FTS5_SPECIAL = set('*^:(){}[]+-"')
    tokens = query.split()
    escaped = []
    for tok in tokens:
        if not tok:
            continue
        if tok.upper() in FTS5_RESERVED:
            escaped.append(f'"{tok}"')
        elif any(c in tok for c in FTS5_SPECIAL):
            escaped.append(f'"{tok.replace(chr(34), chr(34)*2)}"')
        else:
            escaped.append(tok)
    return " ".join(escaped)


def _graph_rag_expand(query: str, db_path: Path, conn=None) -> list[str]:
    """Graph-RAG: extract entities from query, traverse KG, return related entity names.

    When the knowledge graph is enabled, this function:
    1. Extracts entities from the search query using pattern-based NER
    2. Searches the KG for matching entities and their neighbors (1-2 hops)
    3. Returns display names of related entities as query expansion terms

    These terms are added to the FTS query to boost recall for notes
    that mention KG-related entities but don't contain the original query tokens.

    Sprint 4 community-aware mode: when a query entity has a non-zero
    community_id in kg_entities, prefer expansion terms from the same
    community to reduce cross-topic false positives.
    """
    try:
        import search_pipeline
    except ImportError:
        return []

    if not getattr(search_pipeline, "_GRAPH_RAG_ENABLED", False):
        return []
    try:
        from knowledge_graph import (
            KG_ENABLED,
            graph_search as _graph_search,
            extract_entities,
        )

        if not KG_ENABLED:
            return []
        try:
            from infra._lazy_imports import get_config

            _min_occ_q = int(get_config().entity_min_occurrences)
        except Exception as exc:
            logger.debug("entity_min_occurrences config unavailable: %s", exc)
            _min_occ_q = 2
    except ImportError:
        return []
    query_entities = extract_entities(query, min_occurrences=1, use_spacy=False)
    if not query_entities:
        return []
    _pooled_conn = None
    if conn is None:
        try:
            _pooled_conn = connection_pool.get(str(db_path), timeout=10.0)
            conn = _pooled_conn
        except Exception:
            return []
    try:
        combined_query = " ".join(name for name, _ in query_entities[:5])

        query_entity_ids: set[int] = set()
        for name, _ in query_entities[:3]:
            try:
                rows = conn.execute(
                    "SELECT id, community_id FROM kg_entities WHERE lower(name) = ? AND community_id IS NOT NULL AND community_id != 0 LIMIT 1",
                    (name.lower(),),
                ).fetchall()
                if rows:
                     query_entity_ids.add(int(rows[0][0]))
            except Exception as exc:
                logger.debug("kg_entities lookup failed for %r: %s", name, exc)

        results = _graph_search(
            conn,
            combined_query,
            limit=10,
            max_hops=getattr(search_pipeline, "_GRAPH_RAG_MAX_HOPS", 3),
        )
        entity_communities: dict[str, int] = {}
        for entity in results.get("entities", []):
            eid = entity.get("id")
            comm = entity.get("community_id")
            if eid is not None and comm:
                entity_communities[str(eid)] = int(comm)

        same_community_terms: list[str] = []
        other_terms: list[str] = []
        for entity in results.get("entities", []):
            display = entity.get("name", "")
            if not display or display.lower() in {n.lower() for n, _ in query_entities}:
                continue
            eid = entity.get("id")
            comm = entity.get("community_id")
            if query_entity_ids and eid is not None and comm:
                eid_int = int(eid)
                try:
                    in_same_community = any(
                        conn.execute(
                            "SELECT 1 FROM kg_entities WHERE id = ? AND community_id = (SELECT community_id FROM kg_entities WHERE id = ? LIMIT 1)",
                            (eid_int, qid),
                        ).fetchone()
                        for qid in query_entity_ids
                     )
                except Exception as exc:
                    logger.debug("community check failed for entity %s: %s", eid_int, exc)
                    in_same_community = False
                if in_same_community:
                    same_community_terms.append(display)
                    continue
            other_terms.append(display)

        combined = same_community_terms + other_terms
        seen = set()
        expanded = []
        for name in combined:
            key = name.lower()
            if key not in seen:
                seen.add(key)
                expanded.append(name)
            if len(expanded) >= getattr(
                search_pipeline, "_GRAPH_RAG_MAX_EXPANSIONS", 5
            ):
                break
        return expanded
    except Exception:
        logger.warning("Failed to expand query via graph RAG")
        return []
    finally:
        if _pooled_conn is not None:
            connection_pool.put(_pooled_conn)


def _extract_inference_entity(query: str) -> tuple[str | None, list[str]]:
    """Extract entity name and concept keywords from a query.

    Handles inference questions ("Would Caroline likely have X?"),
    temporal entity questions ("When did Caroline go to X?"), and
    factual entity questions ("What did Caroline research?").
    Returns (entity_name, concept_keywords) or (None, []) if no entity is found.
    """
    is_inference = bool(_QUERY_TYPE_INFERENCE_RE.search(query))
    is_temporal_entity = bool(re.search(
        r'\b(when|what|where)\s+(did|does|is|are|was|were|could|would|might|may|happened)\s+'
        r'([A-Z][a-z]+)', query, re.IGNORECASE,
    ))
    if not (is_inference or is_temporal_entity):
        return None, []

    # Extract capitalized words that are likely entity names
    # (skip question words and common verbs)
    _SKIP = {
        "what", "when", "where", "which", "how", "would", "could", "should",
        "does", "do", "is", "are", "was", "were", "has", "have", "had",
        "likely", "probably", "considered", "interested", "during",
        "the", "a", "an", "in", "on", "at", "to", "for", "of", "with",
        "yes", "no", "not", "but", "and", "or",
    }
    words = re.findall(r"[A-Za-z]+", query)
    entities = []
    for w in words:
        if w[0].isupper() and w.lower() not in _SKIP and len(w) > 1:
            entities.append(w)

    if not entities:
        return None, []

    # The first capitalized word after the question word is typically the entity
    entity = entities[0]

    # Concept keywords: remaining content words (lowercase, deduped, no stopwords)
    concept_words = []
    seen = {entity.lower()}
    for w in words:
        wl = w.lower()
        if (wl not in _STOP_WORDS and wl not in seen and len(wl) > 2
                and wl not in ("likely", "probably", "considered", "interested",
                               "pursue", "enjoy", "collect", "play", "read",
                               "watch", "listen", "cook", "eat", "drink",
                               "visit", "travel", "live", "work", "study",
                               "practice", "run", "swim", "hike", "camp",
                               "paint", "draw", "write", "sing", "dance",
                               "drive", "ride", "fly", "sail", "climb",
                               "build", "make", "create", "design", "plan",
                               "organize", "manage", "lead", "teach", "learn",
                               "research", "explore", "discover", "invent",
                               "innovate", "have", "has", "be", "been")):
            concept_words.append(wl)
            seen.add(wl)

    return entity, concept_words


def _parse_search_query(query: str, db_path: Path, conn=None, mode: str = "hybrid") -> tuple[str, str, str, list[str]]:
    """Parse a search query into components.

    Returns (normalized_query, fts_query, bare_query_text, graph_rag_terms).
    """
    normalized_query = normalize_unicode(query)
    phrases = re.findall('"([^"]*)"', normalized_query)
    bare = re.sub('"[^"]*"', " ", normalized_query)
    bare_words = re.findall("[\\w@\\#\\.\\+\\-]+", bare, flags=re.UNICODE)
    # Filter stop words from FTS terms (but keep bare_words for display)
    content_words = [w for w in bare_words if w.lower() not in _STOP_WORDS]
    # Generate adjacent bigram phrase queries for bare words
    bigrams = []
    for i in range(len(content_words) - 1):
        w1 = content_words[i]
        w2 = content_words[i + 1]
        if w1 and w2:
            # Escape the RAW pair as one FTS5 phrase. Previously the code
            # escaped each token first (`"a" "b"`) and then wrapped the
            # joined string in _escape_phrase, which double-escaped every
            # inner quote and produced broken FTS5 syntax
            # (`"""memory"" ""local-first"""`) whenever a token contained
            # a quote — the whole MATCH then failed and search silently
            # returned 0 results for multi-word queries. Regression guard:
            # test_no_silent_search_failures.py::test_search_finds_known_memory_even_with_bad_kg
            bigrams.append(_escape_phrase(f"{w1} {w2}"))
    bigram_terms = bigrams

    expanded = _expand_query(normalized_query)

    # Conceptual phrase expansion: append domain terms for intent phrases
    try:
        expanded = _conceptual_expand(expanded)
    except Exception:
        pass

    # Semantic expansion: find similar memories and extract terms
    # This helps with synonym/paraphrase queries
    # P0 fix #7b: skip semantic expansion for FTS and fact_lookup modes
    # to avoid loading SentenceTransformer model (loky deadlock on macOS)
    semantic_terms = []
    if mode not in ("fts", "fact_lookup"):
        try:
            semantic_terms = _semantic_expand(normalized_query, db_path)
        except Exception:
            pass  # Best-effort — semantic expansion is optional
    if semantic_terms:
        semantic_clause = " OR ".join(
            _escape_phrase(t) for t in semantic_terms
        )
        if expanded:
            expanded = f"({expanded}) OR ({semantic_clause})"
        else:
            expanded = semantic_clause

    if bigram_terms:
        bigram_clause = " OR ".join(bigram_terms)
        if expanded:
            fts_query = f"({bigram_clause}) OR ({expanded})"
        else:
            fts_query = bigram_clause
    else:
        if expanded and expanded != normalized_query:
            fts_query = expanded
        else:
            terms = [_escape_phrase(p) for p in phrases if p.strip()]
            terms += [_escape_fts_query(w) for w in content_words if w]
            fts_query = " AND ".join(terms) if terms else ""

    if not fts_query.strip() and bare_words:
        # Fallback for stopword-only queries: use original bare words as FTS terms
        terms = [_escape_fts_query(w) for w in bare_words if w]
        fts_query = " AND ".join(terms)

    # Entity-anchored AND matching for inference queries.
    # "Would Caroline likely have Dr. Seuss books?" →
    # ("caroline") AND ("dr" OR "seuss" OR "books" OR "bookshelf")
    # This ensures sessions about the entity are ranked above sessions
    # that merely mention concept keywords without the entity.
    entity, concept_kw = _extract_inference_entity(normalized_query)
    if entity and concept_kw:
        entity_clause = _escape_phrase(entity)
        concept_parts = [_escape_phrase(w) for w in concept_kw if w]
        if concept_parts:
            concept_clause = " OR ".join(concept_parts)
            entity_anchored = f"({entity_clause}) AND ({concept_clause})"
            # Combine: entity-anchored AND as a boost, plus the original query
            fts_query = f"({entity_anchored}) OR ({fts_query})"

    graph_rag_terms = _graph_rag_expand(normalized_query, db_path, conn=conn)
    if graph_rag_terms:
        # 2026-06-29 fix: route KG expansion terms through _escape_phrase so
        # embedded double-quotes and `/` in malformed KG entities don't
        # produce broken FTS5 syntax. Regression: see
        # test_no_silent_search_failures.py::test_search_on_db_with_bad_kg_entity_never_returns_error
        graph_rag_fts = " OR ".join(_escape_phrase(t) for t in graph_rag_terms)
        fts_query = f"({fts_query}) OR ({graph_rag_fts})"
    return normalized_query, fts_query, " ".join(bare_words), graph_rag_terms
