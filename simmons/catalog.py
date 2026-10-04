"""Cached OpenRouter model catalog and structural model checks."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from .types import Catalog, ModelInfo, ModelSpec

API_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_CACHE_PATH = Path(".simmons/openrouter-catalog.json")
CACHE_TTL_SECONDS = 24 * 60 * 60
CACHE_SCHEMA_VERSION = 1
_STRUCTURAL_ONLY_NOTICE = (
    "OpenRouter model checks are structural only because no catalog cache is available."
)

JsonObject = Mapping[str, Any]
HttpGet = Callable[[str, Mapping[str, str]], JsonObject]


class _CachedCatalog:
    """Immutable catalog view backed by one timestamped cache snapshot."""

    def __init__(
        self,
        models: Mapping[str, ModelInfo],
        fetched_at: float | None,
        clock: Callable[[], float],
    ) -> None:
        self._models = dict(models)
        self._fetched_at = fetched_at
        self._clock = clock

    def lookup(self, slug: str) -> ModelInfo | None:
        return self._models.get(slug)

    def age_seconds(self) -> float | None:
        if self._fetched_at is None:
            return None
        return max(0.0, self._clock() - self._fetched_at)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed < 0 or parsed == float("inf") or parsed != parsed:
        return None
    return parsed


def _price_per_million(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return float(parsed * Decimal(1_000_000))


def _string_set(value: Any) -> frozenset[str]:
    if not isinstance(value, (list, tuple)):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str))


def _model_info(record: JsonObject) -> ModelInfo:
    model = record.get("model")
    endpoints = record.get("endpoints")
    if not isinstance(model, Mapping):
        raise ValueError("catalog entry is missing its model object")
    slug = model.get("id")
    if not isinstance(slug, str) or not slug:
        raise ValueError("catalog model is missing its id")
    if not isinstance(endpoints, list):
        endpoints = []
    raw_context = model.get("context_length")
    context_length = _number(raw_context)
    if context_length is None:
        endpoint_contexts = [
            parsed
            for endpoint in endpoints
            if isinstance(endpoint, Mapping)
            if (parsed := _number(endpoint.get("context_length"))) is not None and parsed > 0
        ]
        context_length = min(endpoint_contexts, default=0)

    endpoint_parameters = [
        (_endpoint_host(endpoint), _string_set(endpoint.get("supported_parameters")))
        for endpoint in endpoints
        if isinstance(endpoint, Mapping)
    ]
    supported_parameters: frozenset[str] = frozenset()
    if endpoint_parameters:
        common = set(endpoint_parameters[0][1])
        for _host, parameters in endpoint_parameters[1:]:
            common.intersection_update(parameters)
        supported_parameters = frozenset(common)

    endpoint_prices: list[tuple[str, float, float]] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, Mapping):
            continue
        pricing = endpoint.get("pricing")
        if not isinstance(pricing, Mapping):
            continue
        input_price = _price_per_million(pricing.get("prompt"))
        output_price = _price_per_million(pricing.get("completion"))
        if input_price is None or output_price is None:
            continue
        host = _endpoint_host(endpoint)
        if not host:
            continue
        endpoint_prices.append((host, input_price, output_price))

    return ModelInfo(
        slug=slug,
        context_length=int(context_length),
        supports_tools="tools" in supported_parameters,
        endpoint_prices=tuple(endpoint_prices),
        supported_parameters=supported_parameters,
        endpoint_parameters=tuple((host, parameters) for host, parameters in endpoint_parameters if host),
    )


def _endpoint_host(endpoint: JsonObject) -> str:
    host = endpoint.get("tag") or endpoint.get("provider_name")
    return host if isinstance(host, str) else ""


def _names(value: Any) -> list[str] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item.lower() for item in value if isinstance(item, str)]
    return None


def _host_matches(host: str, names: list[str]) -> bool:
    host = host.lower()
    return host in names or host.split("/", 1)[0] in names


def route_parameters(
    info: ModelInfo, provider_params: Mapping[str, Any] | None = None
) -> tuple[frozenset[str], frozenset[str]]:
    """Return (supported by every routed endpoint, supported by any routed endpoint).

    Routed endpoints are the ones ``provider.only`` or ``provider.order`` names,
    matched by endpoint tag or its provider prefix, minus ``provider.ignore``.
    Without a pin, every endpoint is routed. A pin that matches no catalog
    endpoint falls back to every endpoint.
    """
    endpoints = list(info.endpoint_parameters)
    if not endpoints:
        return info.supported_parameters, info.supported_parameters
    routing = provider_params if isinstance(provider_params, Mapping) else {}
    pinned = _names(routing.get("only"))
    if pinned is None:
        pinned = _names(routing.get("order"))
    if pinned:
        selected = [item for item in endpoints if _host_matches(item[0], pinned)]
        endpoints = selected or endpoints
    ignored = _names(routing.get("ignore"))
    if ignored:
        endpoints = [item for item in endpoints if not _host_matches(item[0], ignored)] or endpoints
    every = set(endpoints[0][1])
    some: set[str] = set()
    for _host, parameters in endpoints:
        every.intersection_update(parameters)
        some.update(parameters)
    return frozenset(every), frozenset(some)


def _load_catalog(path: Path, clock: Callable[[], float]) -> Catalog:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _CachedCatalog({}, None, clock)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read OpenRouter catalog cache {path}: {exc}") from exc

    if not isinstance(snapshot, Mapping):
        raise ValueError(f"OpenRouter catalog cache {path} must be a JSON object")
    if snapshot.get("schema_version") != CACHE_SCHEMA_VERSION:
        raise ValueError(f"unsupported OpenRouter catalog cache schema in {path}")
    fetched_at = _number(snapshot.get("fetched_at"))
    if fetched_at is None:
        raise ValueError(f"OpenRouter catalog cache {path} has no valid fetched_at timestamp")
    entries = snapshot.get("models")
    if not isinstance(entries, list):
        raise ValueError(f"OpenRouter catalog cache {path} has no models list")

    models: dict[str, ModelInfo] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError(f"OpenRouter catalog cache {path} contains an invalid model entry")
        info = _model_info(entry)
        models[info.slug] = info
    return _CachedCatalog(models, fetched_at, clock)


def load_catalog(path: str | Path | None = None) -> Catalog:
    """Load a cached catalog without network access."""
    return _load_catalog(Path(path) if path is not None else DEFAULT_CACHE_PATH, time.time)


def _endpoint_url(model: JsonObject) -> str:
    links = model.get("links")
    details = links.get("details") if isinstance(links, Mapping) else None
    if isinstance(details, str) and details:
        parsed = urlparse(details)
        if parsed.netloc and parsed.netloc not in {"openrouter.ai", "www.openrouter.ai"}:
            raise ValueError("OpenRouter model details link points outside openrouter.ai")
        path = details if parsed.netloc else urljoin(f"{API_BASE_URL}/", details)
        if path.endswith("/endpoints"):
            return path
        return f"{path.rstrip('/')}/endpoints"

    slug = model.get("id")
    if not isinstance(slug, str) or "/" not in slug:
        raise ValueError(f"OpenRouter model is missing a valid id: {slug!r}")
    author, model_name = slug.split("/", 1)
    return (
        f"{API_BASE_URL}/models/{quote(author, safe='')}/"
        f"{quote(model_name, safe='')}/endpoints"
    )


def _http_get_json(url: str, headers: Mapping[str, str]) -> JsonObject:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 403 and url.endswith("/endpoints"):
            raise RuntimeError(
                "OpenRouter denied endpoint-list access; set OPENROUTER_MANAGEMENT_KEY."
            ) from None
        raise RuntimeError(f"OpenRouter catalog request failed with HTTP {exc.code}.") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"OpenRouter catalog request failed: {exc}") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("OpenRouter returned invalid JSON for its catalog.") from None
    if not isinstance(result, Mapping):
        raise RuntimeError("OpenRouter returned an invalid catalog response.")
    return result


def _write_snapshot(path: Path, snapshot: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = stream.name
            json.dump(snapshot, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def refresh_catalog(
    path: str | Path | None = None,
    *,
    force: bool = False,
    now: Callable[[], float] = time.time,
    http_get: HttpGet | None = None,
    api_key: str | None = None,
    management_key: str | None = None,
) -> Catalog:
    """Reuse a fresh cache or fetch models and each model's endpoint list."""
    cache_path = Path(path) if path is not None else DEFAULT_CACHE_PATH
    cached = _load_catalog(cache_path, now)
    age = cached.age_seconds()
    if not force and age is not None and age < CACHE_TTL_SECONDS:
        return cached

    api_token = api_key or os.environ.get("OPENROUTER_API_KEY")
    management_token = management_key or os.environ.get("OPENROUTER_MANAGEMENT_KEY")
    if not api_token:
        api_token = management_token
    if not api_token:
        raise RuntimeError("OPENROUTER_API_KEY is required to refresh the catalog.")

    get_json = http_get or _http_get_json
    models_response = get_json(
        f"{API_BASE_URL}/models", {"Authorization": f"Bearer {api_token}"}
    )
    listed_models = models_response.get("data")
    if not isinstance(listed_models, list):
        raise RuntimeError("OpenRouter returned a models response without a data list.")

    endpoint_token = management_token or api_token
    endpoint_headers = {"Authorization": f"Bearer {endpoint_token}"}
    stored_models: list[dict[str, Any]] = []
    for model in listed_models:
        if not isinstance(model, Mapping):
            raise RuntimeError("OpenRouter returned an invalid model record.")
        endpoint_response = get_json(_endpoint_url(model), endpoint_headers)
        endpoint_data = endpoint_response.get("data")
        endpoints = endpoint_data.get("endpoints") if isinstance(endpoint_data, Mapping) else None
        if not isinstance(endpoints, list):
            raise RuntimeError(
                f"OpenRouter returned an endpoint response without a list for {model.get('id')!r}."
            )
        stored_models.append({"model": dict(model), "endpoints": endpoints})

    snapshot = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "fetched_at": float(now()),
        "models": stored_models,
    }
    _write_snapshot(cache_path, snapshot)
    return _load_catalog(cache_path, now)


def check_models(
    models: Mapping[str, ModelSpec],
    catalog: Catalog | None,
    *,
    projected_input_tokens: Mapping[str, int] | None = None,
) -> tuple[str, ...]:
    """Validate configured OpenRouter models, or report structural-only mode.

    ``models`` maps role names to their resolved specs. The optional token
    projection is the largest request for each role before ``max_tokens``.
    Provider-routing preferences are OpenRouter controls, not model params, so
    they are not compared with endpoint ``supported_parameters``.
    """
    openrouter_models = {
        role: spec for role, spec in models.items() if spec.provider == "openrouter"
    }
    if not openrouter_models:
        return ()
    if catalog is None or catalog.age_seconds() is None:
        return (_STRUCTURAL_ONLY_NOTICE,)

    errors: list[str] = []
    for role, spec in openrouter_models.items():
        path = f"models.{role}"
        info = catalog.lookup(spec.model)
        if info is None:
            errors.append(f"{path}: unknown OpenRouter model slug {spec.model!r}.")
            continue
        if not info.supports_tools:
            errors.append(f"{path}: model {spec.model!r} does not support tools.")
        if not info.endpoint_prices:
            errors.append(f"{path}: model {spec.model!r} has no priced endpoints.")

        model_params = set(spec.params) - {"provider"}
        routed, _ = route_parameters(info, spec.params.get("provider"))
        for parameter in sorted(model_params - routed):
            errors.append(
                f"{path}.params.{parameter}: model {spec.model!r} does not support "
                "this parameter on every routed endpoint."
            )

        if projected_input_tokens is not None and role in projected_input_tokens:
            input_tokens = projected_input_tokens[role]
            if input_tokens < 0:
                errors.append(f"{path}: projected input token count cannot be negative.")
            else:
                required = input_tokens + spec.max_tokens
                if required > info.context_length:
                    errors.append(
                        f"{path}: request cannot fit model context; projected input "
                        f"{input_tokens} + max_tokens {spec.max_tokens} = {required}, "
                        f"but {spec.model!r} provides {info.context_length}. Set log_window "
                        "or reduce the request size."
                    )

    if errors:
        raise ValueError("\n".join(errors))
    return ()


__all__ = [
    "API_BASE_URL",
    "CACHE_TTL_SECONDS",
    "Catalog",
    "ModelInfo",
    "check_models",
    "load_catalog",
    "refresh_catalog",
    "route_parameters",
]
