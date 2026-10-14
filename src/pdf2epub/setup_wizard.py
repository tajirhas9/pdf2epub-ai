"""`pdf2epub init`: first-run configuration for people who installed the tool (no repo, no .env)."""

from __future__ import annotations

from dataclasses import dataclass

import typer
from rich.console import Console

from pdf2epub import config_store
from pdf2epub.config import LLMSettings, keyring_set


@dataclass(frozen=True)
class Provider:
    label: str
    base_url: str
    needs_key: bool
    hint: str = ""


PROVIDERS: dict[str, Provider] = {
    "llamacpp": Provider("llama.cpp server (local)", "http://localhost:8080/v1", False),
    "lmstudio": Provider("LM Studio (local)", "http://localhost:1234/v1", False),
    "ollama": Provider("Ollama (local)", "http://localhost:11434/v1", False),
    "openai": Provider("OpenAI", "https://api.openai.com/v1", True),
    "gemini": Provider("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai/", True),
    "openrouter": Provider("OpenRouter", "https://openrouter.ai/api/v1", True, "many vision models, one key"),
    "custom": Provider("Other OpenAI-compatible endpoint", "", False),
    "none": Provider("No LLM (deterministic conversion only)", "", False),
}


def run_wizard(
    console: Console,
    provider: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    use_keyring: bool = True,
    yes: bool = False,
) -> dict:
    from pdf2epub.llm.probe import list_models

    data = config_store.read()
    llm = data.setdefault("llm", {})

    if provider is None:
        console.print("[bold]LLM provider[/] (checks every page against its image; optional)")
        keys = list(PROVIDERS)
        for i, k in enumerate(keys, 1):
            p = PROVIDERS[k]
            console.print(f"  {i}. {p.label}" + (f"  [dim]{p.hint}[/]" if p.hint else ""))
        choice = typer.prompt("Choose", default="1")
        provider = keys[int(choice) - 1] if choice.isdigit() else choice
    if provider not in PROVIDERS:
        raise typer.BadParameter(f"unknown provider {provider!r}; one of {', '.join(PROVIDERS)}")
    p = PROVIDERS[provider]

    if provider == "none":
        llm["enabled"] = False
    else:
        llm["enabled"] = True
        llm["base_url"] = base_url or (
            p.base_url
            if yes and p.base_url
            else typer.prompt("Base URL", default=p.base_url or llm.get("base_url", ""))
        )
        key_needed = p.needs_key or (
            provider == "custom" and not yes and typer.confirm("Does it need an API key?", default=False)
        )
        if key_needed and api_key is None and not yes:
            api_key = typer.prompt("API key (input hidden)", hide_input=True)
        if api_key:
            if use_keyring and keyring_set(llm["base_url"], api_key):
                llm.pop("api_key", None)
                console.print("API key stored in the OS keyring.")
            else:
                llm["api_key"] = api_key
                console.print(
                    f"[yellow]No OS keyring available: API key stored in {config_store.config_path()} (mode 600).[/]"
                )
        probe_settings = LLMSettings(base_url=llm["base_url"], api_key=api_key or None)
        models = list_models(probe_settings)
        if model is None and not yes:
            if models:
                shown = models[:30]
                console.print(
                    "Models offered by the endpoint"
                    + (f" (first {len(shown)} of {len(models)})" if len(models) > 30 else "")
                    + ":"
                )
                for m in shown:
                    console.print(f"  {m}")
            default = models[0] if len(models) == 1 else llm.get("model", "")
            model = typer.prompt(
                "Model (needs image input; blank = server default)", default=default, show_default=bool(default)
            )
        if model is not None:
            if model:
                llm["model"] = model
            else:
                llm.pop("model", None)

    if not yes:
        langs = typer.prompt(
            "Default language hints, e.g. en,ar (blank = auto-detect)",
            default=",".join(data.get("langs", [])),
            show_default=False,
        )
        data["langs"] = [x.strip() for x in langs.split(",") if x.strip()]
        out = typer.prompt(
            "Default output directory (blank = current directory)", default=data.get("out_dir", ""), show_default=False
        )
        if out.strip():
            data["out_dir"] = out.strip()
        else:
            data.pop("out_dir", None)
    if not data.get("langs"):
        data.pop("langs", None)
    path = config_store.write(data)
    console.print(f"[green]saved[/] {path}")
    return data
