"""Road Inspection Assistant: natural-language, read-only questions over RoadSense records.

    question -> understanding (QuerySpec, validated) -> retrieval (fixed query functions over the core modules)
             -> response (answer built from the records; optional LLM summary, checked against them) -> stored turn

The LLM never writes SQL, never calls tools and never sees raw records: it turns a question into a validated
QuerySpec, and (optionally) summarises a compact, PII-free context whose ids and numbers are checked afterwards.
"""
