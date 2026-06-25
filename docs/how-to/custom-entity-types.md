# How to Extend Entity Types

Agentic Memory uses regex-based NER to extract entities. You can add **custom entity types** for your domain.

## Default Entity Types

| Type | Pattern | Examples |
|------|---------|----------|
| `technology` | `\b(Python\|SQLite\|Docker\|...)\b` | Python, PostgreSQL, React |
| `concept` | `\b(auth\|caching\|migration\|...)\b` | authentication, deployment |
| `file` | `\b([a-zA-Z0-9_/.-]+\.(py\|js\|...))\b` | src/main.py, config.yaml |
| `command` | `\b(git\|npm\|pip)\s+\S+` | git commit, pip install |
| `url` | `https?://[^\s]+` | https://example.com |
| `email` | `[a-zA-Z0-9._%+-]+@...` | user@example.com |

## Adding Custom Entity Types

### Step 1: Edit `knowledge_graph.py`

Find the `ENTITY_PATTERNS` dictionary and add your custom type:

```python
# knowledge_graph.py
ENTITY_PATTERNS = {
    # ... existing patterns ...
    
    # Add your custom type
    "aws_service": r"\b(Lambda|S3|DynamoDB|CloudFront|SQS|SNS|EC2|ECS|EKS)\b",
    "database": r"\b(PostgreSQL|MySQL|MongoDB|Redis|Elasticsearch|Cassandra)\b",
    "api_endpoint": r"\b(/api/v[0-9]+/[a-zA-Z0-9/_-]+)\b",
    "env_var": r"\b([A-Z][A-Z0-9_]{2,})\b",
}
```

### Step 2: Rebuild the Knowledge Graph

After adding patterns, rebuild the KG to extract new entities:

```bash
python backfill_all.py --mode full
```

### Step 3: Verify

```bash
# Check extracted entities
python -c "
import sqlite3
conn = sqlite3.connect('memory.db')
for row in conn.execute(
    \"SELECT entity_type, COUNT(*) FROM kg_entities GROUP BY entity_type ORDER BY COUNT(*) DESC\"
):
    print(f'{row[0]}: {row[1]} entities')
"
```

## Example: AWS Entity Types

```python
# In knowledge_graph.py
ENTITY_PATTERNS = {
    "aws_service": r"\b(Lambda|S3|DynamoDB|CloudFront|SQS|SNS|EC2|ECS|EKS|IAM|VPC|Route53|CloudWatch)\b",
    "aws_resource": r"\b(arn:aws:[a-zA-Z0-9:/_-]+)\b",
    "aws_region": r"\b(us-east-1|us-west-2|eu-west-1|ap-southeast-1)\b",
}
```

## Example: API Entity Types

```python
ENTITY_PATTERNS = {
    "api_endpoint": r"\b(/api/v[0-9]+/[a-zA-Z0-9/_{}-]+)\b",
    "http_method": r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b",
    "status_code": r"\b([1-5][0-9]{2})\b",
}
```

## Example: Project-Specific Entities

```python
ENTITY_PATTERNS = {
    "internal_service": r"\b(auth-service|payment-service|notification-service)\b",
    "team": r"\b(platform-team|backend-team|frontend-team|ml-team)\b",
    "feature_flag": r"\b(flag_[a-z_]+|feature_[a-z_]+)\b",
}
```

## Querying Custom Entities

Once extracted, you can search for custom entities:

```python
from agentic_memory import search_graph

# Find all AWS services mentioned
aws_entities = search_graph("*", entity_type="aws_service", limit=50)

# Find memories mentioning Lambda
lambda_memories = search_graph("Lambda", max_hops=1)
```

## Limitations

- **Regex-based** — Can't handle complex entity structures
- **Case-sensitive by default** — Use `\b` word boundaries for flexibility
- **No context understanding** — "Python" the language vs "python" the command
- **Performance** — More patterns = slower extraction

## Further Reading

- [Knowledge Graph](../concepts/knowledge-graph.md) — How NER works
- [Search Pipeline](../concepts/search-pipeline.md) — How entities are used in search
