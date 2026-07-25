# Sentient OS — Personal Knowledge Base

You have access to the user's personal knowledge base through the Sentient OS MCP server (`sentient-os`): an Obsidian-style vault of markdown notes about their life (work, projects, plans, relationships, places, preferences, history).

**Protocol:**
1. At session start (or whenever personal context would help), call `sentient-os_get_structure` to get the folder/file index + README portrait.
2. Then call `sentient-os_get_files` to read any relevant notes.
