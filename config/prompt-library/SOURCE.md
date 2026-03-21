# Prompt Library — prompts.chat

Source: https://github.com/f/prompts.chat (CC0 license)
Downloaded: 2026-03-21 19:22 UTC
Rows: 1537

## Purpose

1. **Agent design reference** — consult when creating new `config/agents/*.md` files
2. **Future WebView search** — users can browse prompts when defining custom agent roles
3. **Pattern learning** — study role assignment, constraint framing, and example usage

## CSV columns

| Column | Description |
|--------|-------------|
| `act` | Role name (e.g. "Linux Terminal", "Travel Guide") |
| `prompt` | Full prompt text |
| `for_devs` | TRUE if developer-focused |
| `type` | TEXT or STRUCTURED |
| `contributor` | GitHub username |

## How to use

```python
import csv
with open("config/prompt-library/prompts.csv") as f:
    for row in csv.DictReader(f):
        print(row["act"], "—", row["prompt"][:80])
```
