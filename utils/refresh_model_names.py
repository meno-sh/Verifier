"""Rebuild utils/model_names.txt, the model names the consistency checks look for.

    uv run utils/refresh_model_names.py

The names come from the Hugging Face Hub (the most downloaded text-generation and
image-text-to-text models) and from OpenRouter's model list, which adds models that
are not on the Hub, such as GPT-4o and Claude. The checks read only the saved file,
so a verdict never depends on the network. Run this now and then to pick up new
models, and commit the result.
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent / "model_names.txt"
HUB = ("https://huggingface.co/api/models?pipeline_tag={tag}&sort=downloads&direction=-1"
       "&limit=1000")
HUB_TAGS = {"text-generation": 5, "image-text-to-text": 1}     # tag: pages of 1000
OPENROUTER = "https://openrouter.ai/api/v1/models"
# Suffixes for file formats and quantisations, which a paper never names.
FORMAT_SUFFIX = re.compile(r"[-_.](gguf|awq|gptq|exl2|mlx|onnx|fp8|fp16|bf16|int4|int8|"
                           r"\d+bit|bnb-\dbit|q\d_k_m|dynamic)$", re.I)
# Suffixes for a tuned variant. Papers often name the base model ("Llama-3.3-70B")
# when the Hub lists only the variant, so the name without the suffix is added too.
VARIANT_SUFFIX = re.compile(r"[-_](instruct|chat|it|base|hf)$", re.I)


def fetch(url: str) -> tuple[object, str | None]:
    """The JSON at `url`, and the next page's URL from its Link header, if any."""
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "verifier"}),
                                timeout=60) as r:
        link = r.headers.get("Link", "")
        nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
        return json.load(r), nxt.group(1) if nxt else None


def hub_names() -> set[str]:
    """Model names (the part after the owner) of the Hub's most downloaded models."""
    names = set()
    for tag, pages in HUB_TAGS.items():
        url = HUB.format(tag=tag)
        for _ in range(pages):
            models, url = fetch(url)
            names |= {m["id"].split("/")[-1] for m in models}
            if url is None:
                break
    return names


def openrouter_names() -> set[str]:
    """Model names from OpenRouter, without the provider and any ":variant"."""
    models, _ = fetch(OPENROUTER)
    return {m["id"].split("/")[-1].split(":")[0] for m in models["data"]}


def usable(name: str) -> bool:
    """A name the checks can look for without matching ordinary words or numbers."""
    return (len(name) >= 4 and any(c.isdigit() for c in name)
            and any(c.isalpha() for c in name) and " " not in name)


def main() -> int:
    """Fetch, clean and write the names, one per line, sorted."""
    found = set()
    for name in hub_names() | openrouter_names():
        while FORMAT_SUFFIX.search(name):
            name = FORMAT_SUFFIX.sub("", name)
        found |= {name, VARIANT_SUFFIX.sub("", name)}
    spelling: dict[str, str] = {}            # one spelling per name, ignoring case
    for name in sorted(found, key=lambda n: (n.lower(), n == n.lower())):
        if usable(name):
            spelling.setdefault(name.lower(), name)
    names = sorted(spelling.values(), key=str.lower)
    OUT.write_text("# Model names for the consistency checks; rebuilt by "
                   "utils/refresh_model_names.py.\n" + "".join(f"{n}\n" for n in names))
    print(f"wrote {len(names)} names to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
