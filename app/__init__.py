"""Ollama Token Logger - a transparent, async Ollama proxy that records token
usage and estimates equivalent OpenAI API cost.

The proxy never rewrites Ollama's native API (including streaming, reasoning
``thinking`` fields, and tool calls). It only tees a bounded tail of each
inference response to extract the usage statistics Ollama already reports.
"""

__version__ = "1.0.0"
