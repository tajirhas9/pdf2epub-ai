"""Quick end-to-end check of the configured endpoint: auth, model, vision, JSON output."""

from __future__ import annotations

from PIL import Image, ImageDraw

from pdf2epub.config import LLMSettings
from pdf2epub.llm.client import ImagePart, LLMError, OpenAICompatClient

SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "color": {"type": "string"}},
    "required": ["text", "color"],
}


async def probe(settings: LLMSettings) -> str:
    img = Image.new("RGB", (320, 120), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle((10, 10, 60, 60), fill="red")
    draw.text((90, 40), "HELLO 42", fill="black")
    client = OpenAICompatClient(settings)
    try:
        await client.ping()  # fail fast on a wrong URL / key / refused host
        out = await client.complete_json(
            "You read images precisely.",
            "What text is written in the image, and what colour is the square? Reply as JSON.",
            [ImagePart.from_pil(img)],
            SCHEMA,
            "probe",
        )
    except LLMError as e:
        return f"[red]✗[/] {e}"
    finally:
        await client.close()
    ok = "42" in str(out.get("text", "")) and "red" in str(out.get("color", "")).lower()
    mode = client._json_mode.replace("_", " ")
    if ok:
        return (
            f"[green]✓[/] connected · reads images correctly · structured output via {mode} "
            f"[dim](model: {client._model})[/]"
        )
    return (
        f"[yellow]![/] connected, but the model misread a test image ({out}). "
        "Reviews may be unreliable — a stronger vision model is recommended."
    )


def list_models(settings: LLMSettings) -> list[str]:
    """Model ids the endpoint offers (empty when the endpoint doesn't support listing)."""
    import openai

    try:
        client = openai.OpenAI(
            base_url=settings.base_url,
            api_key=settings.key_value(),
            timeout=15,
            max_retries=0,
            default_headers=settings.extra_headers or None,
        )
        return sorted(m.id for m in client.models.list())
    except Exception:
        return []
