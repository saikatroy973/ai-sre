"""AI SRE: evidence-backed, read-only Kubernetes investigations."""

import os

__version__ = "0.1.0"

# Compatibility: older agent configs and .env files use the historical
# INVESTIGATOR_* prefix. Copy them to AI_SRE_* without overriding new values.
for _key, _value in list(os.environ.items()):
    if _key.startswith("INVESTIGATOR_"):
        os.environ.setdefault("AI_SRE_" + _key.removeprefix("INVESTIGATOR_"), _value)
