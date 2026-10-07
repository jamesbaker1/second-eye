"""The Anthropic API's contract, as the installed SDK types state it.

Every fake that stands in for Anthropic in this suite was written from the
documentation, and the documentation and our reading of it can both be wrong:
the last live run found six bugs no test had. The SDK's typed request params
(TypedDicts) and response models (pydantic) are generated from the API's own
schema, so they are the strongest evidence of what the real API accepts and
returns that can be had without a network call. This module holds the fakes
to them.

`strict(fake_client)` wraps a fake client. Every call through it is resolved
to the real SDK method of the same path (`client.beta.sessions.create` is
`anthropic.resources.beta.sessions.Sessions.create`), and:

- the arguments are bound to that method's signature, so an argument the SDK
  does not take, or a missing required one, fails the call;
- each argument is checked against its annotation, recursively through the
  TypedDicts, so an unknown field, a missing required field, a wrong literal
  (an enum the API does not have) or a wrong type fails the call;
- the fake's answer is checked against the method's return type, field by
  field, and then turned into the SDK's own model instance the way the real
  client parses a response (`construct_type`). Our code therefore reads the
  same objects it would read live: an attribute the real response does not
  have is an AttributeError here too, and an event the real API would never
  send cannot reach our code.

`check()` is the validator on its own, `model()` builds a valid SDK object
for a fake to return, and `ContractError` is what a violation raises.
"""

from __future__ import annotations

import collections.abc
import contextlib
import functools
import inspect
import typing
from datetime import date, datetime
from types import SimpleNamespace, UnionType

import anthropic
import typing_extensions
from anthropic._models import BaseModel as SDKModel
from anthropic._models import construct_type

_Required = (typing.Required, typing_extensions.Required)
_NotRequired = (typing.NotRequired, typing_extensions.NotRequired)
_ReadOnly = tuple(x for x in (getattr(typing, "ReadOnly", None),
                              getattr(typing_extensions, "ReadOnly", None)) if x)


class ContractError(AssertionError):
    """A request the real API would refuse, or a response it would never send."""


# --------------------------------------------------------------------------
# The validator
# --------------------------------------------------------------------------


def _strip(tp):
    """Annotated[X, ...], Required[X], NotRequired[X], ReadOnly[X] -> X."""
    while True:
        origin = typing.get_origin(tp)
        if origin is typing.Annotated or origin is typing_extensions.Annotated or origin in _Required or origin in _NotRequired or origin in _ReadOnly:
            tp = typing.get_args(tp)[0]
        else:
            return tp


def _is_typeddict(tp) -> bool:
    return typing_extensions.is_typeddict(tp)


def _is_model(tp) -> bool:
    return isinstance(tp, type) and issubclass(tp, SDKModel)


@functools.cache
def _typeddict_fields(tp) -> tuple[dict, frozenset]:
    hints = typing.get_type_hints(tp, include_extras=True)
    required = set()
    total = getattr(tp, "__total__", True)
    for key, hint in hints.items():
        origin = typing.get_origin(hint)
        if origin in _Required or (total and origin not in _NotRequired):
            required.add(key)
    return {k: _strip(v) for k, v in hints.items()}, frozenset(required)


@functools.cache
def _model_fields(tp) -> tuple[dict, frozenset]:
    # Generated models defer their build; their field annotations resolve then.
    with contextlib.suppress(Exception):
        tp.model_rebuild()
    fields = tp.model_fields
    hints = {name: _strip(f.annotation) for name, f in fields.items()}
    # The JSON names: a field with an alias is sent under the alias.
    aliased = {(f.alias or name): hints[name] for name, f in fields.items()}
    required = frozenset((f.alias or name) for name, f in fields.items() if f.is_required())
    return aliased, required


def as_data(value):
    """A fake's answer as plain JSON-shaped data: namespaces and SDK models
    become dicts, recursively."""
    if isinstance(value, SDKModel):
        return {k: as_data(v) for k, v in value.__dict__.items()}
    if isinstance(value, SimpleNamespace):
        return {k: as_data(v) for k, v in vars(value).items()}
    if isinstance(value, dict):
        return {k: as_data(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [as_data(v) for v in value]
    return value


def _name(tp) -> str:
    return getattr(tp, "__name__", None) or str(tp)


def _type_literal(tp) -> set:
    """The values a TypedDict or model's `type` field may take, for picking
    the arm of a discriminated union to report against."""
    try:
        fields = (_typeddict_fields(tp) if _is_typeddict(tp) else _model_fields(tp))[0]
    except Exception:  # noqa: BLE001
        return set()
    hint = fields.get("type")
    if hint is not None and typing.get_origin(hint) is typing.Literal:
        return set(typing.get_args(hint))
    return set()


def check(value, tp, path: str = "$", *, response: bool = False) -> list[str]:
    """Every way `value` departs from `tp`, as readable lines. Empty: it fits.

    `response` is True for what the API sends back: a timestamp may then be
    an RFC 3339 string (it is on the wire), and nothing else is loosened.
    """
    errors: list[str] = []
    _check(value, _strip(tp), path, errors, response)
    return errors


def _check(value, tp, path, errors, response):
    tp = _strip(tp)
    if tp is typing.Any or tp is object:
        return
    if tp is type(None) or tp is None:
        if value is not None:
            errors.append(f"{path}: expected null, got {value!r:.80}")
        return
    if isinstance(tp, typing.TypeVar):
        return
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    if origin is typing.Union or origin is UnionType:
        arms = [_strip(a) for a in args]
        # Omit / NotGiven are the SDK's "not sent" markers, never a value.
        arms = [a for a in arms if not (isinstance(a, type) and a.__name__ in ("Omit", "NotGiven"))]
        if value is None and any(a is type(None) for a in arms):
            return
        best: list[str] | None = None
        kind = value.get("type") if isinstance(value, dict) else None
        for arm in arms:
            arm_errors: list[str] = []
            _check(value, arm, path, arm_errors, response)
            if not arm_errors:
                return
            literal = _type_literal(arm) if kind is not None else set()
            if kind is not None and kind in literal:
                best = arm_errors          # the arm the value says it is
                break
            if best is None or len(arm_errors) < len(best):
                best = arm_errors
        if kind is not None and not any(kind in _type_literal(a) for a in arms
                                        if _is_typeddict(a) or _is_model(a)):
            kinds = sorted(str(k) for a in arms for k in _type_literal(a))
            if kinds:
                errors.append(f"{path}.type: {kind!r} is not one of {kinds}")
                return
        errors.extend(best or [f"{path}: {value!r:.80} fits none of {tp}"])
        return

    if origin is typing.Literal:
        if value not in args:
            errors.append(f"{path}: {value!r:.80} is not one of {list(args)}")
        return

    if _is_typeddict(tp):
        if not isinstance(value, collections.abc.Mapping):
            errors.append(f"{path}: expected an object ({_name(tp)}), got {type(value).__name__}")
            return
        fields, required = _typeddict_fields(tp)
        # PEP 728: `extra_items=X` declares that other keys are allowed (a
        # JSON Schema object's own keywords, say), each of type X.
        extra = getattr(tp, "__extra_items__", typing_extensions.NoExtraItems)
        for key in value:
            if key in fields:
                continue
            if extra is not typing_extensions.NoExtraItems:
                _check(value[key], extra, f"{path}.{key}", errors, response)
            else:
                errors.append(f"{path}.{key}: not a field of {_name(tp)} "
                              f"(fields: {', '.join(sorted(fields))})")
        for key in sorted(required - set(value)):
            errors.append(f"{path}.{key}: required by {_name(tp)}, missing")
        for key, item in value.items():
            if key in fields:
                _check(item, fields[key], f"{path}.{key}", errors, response)
        return

    if _is_model(tp):
        if isinstance(value, SDKModel | SimpleNamespace):
            value = as_data(value)
        if not isinstance(value, collections.abc.Mapping):
            errors.append(f"{path}: expected an object ({_name(tp)}), got {type(value).__name__}")
            return
        fields, required = _model_fields(tp)
        # A model that declares `__pydantic_extra__` (a JSON Schema object)
        # takes other keys as part of its schema.
        open_ended = "__pydantic_extra__" in getattr(tp, "__annotations__", {})
        for key in value:
            if key not in fields and not open_ended:
                errors.append(f"{path}.{key}: not a field of {_name(tp)} "
                              f"(fields: {', '.join(sorted(fields))})")
        for key in sorted(required - set(value)):
            errors.append(f"{path}.{key}: required by {_name(tp)}, missing")
        for key, item in value.items():
            if key in fields:
                _check(item, fields[key], f"{path}.{key}", errors, response)
        return

    if origin in (list, collections.abc.Iterable, collections.abc.Sequence, tuple, set,
                  frozenset) or (isinstance(tp, type) and tp.__name__ == "SequenceNotStr"):
        if isinstance(value, str | bytes) or not isinstance(value, list | tuple | set | frozenset):
            errors.append(f"{path}: expected an array, got {type(value).__name__}")
            return
        item_tp = args[0] if args else typing.Any
        for i, item in enumerate(value):
            _check(item, item_tp, f"{path}[{i}]", errors, response)
        return
    if origin is not None and getattr(origin, "__name__", "") == "SequenceNotStr":
        _check(value, list[args[0]] if args else list, path, errors, response)
        return

    if origin in (dict, collections.abc.Mapping):
        if not isinstance(value, dict):
            errors.append(f"{path}: expected an object, got {type(value).__name__}")
            return
        key_tp, value_tp = args or (str, typing.Any)
        for k, v in value.items():
            _check(k, key_tp, f"{path}<key>", errors, response)
            _check(v, value_tp, f"{path}.{k}", errors, response)
        return

    if tp is bool:
        if not isinstance(value, bool):
            errors.append(f"{path}: expected a boolean, got {value!r:.80}")
        return
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{path}: expected an integer, got {value!r:.80}")
        return
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            errors.append(f"{path}: expected a number, got {value!r:.80}")
        return
    if tp is str:
        if not isinstance(value, str):
            errors.append(f"{path}: expected a string, got {type(value).__name__} {value!r:.80}")
        return
    if tp is datetime or tp is date:
        if isinstance(value, datetime | date):
            return
        if isinstance(value, str):
            try:
                datetime.fromisoformat(value)
                return
            except ValueError:
                pass
        errors.append(f"{path}: expected an RFC 3339 timestamp, got {value!r:.80}")
        return
    # Anything else (FileTypes, httpx types, a pydantic output_format class)
    # is the SDK's own business, not the wire contract.


def require(value, tp, what: str, *, response: bool = False) -> None:
    errors = check(value, tp, response=response)
    if errors:
        raise ContractError(f"{what} breaks the API contract:\n  " + "\n  ".join(errors))


# --------------------------------------------------------------------------
# Building what a fake returns
# --------------------------------------------------------------------------


def model(tp, **fields):
    """An SDK response object of type `tp` (a model, or a union such as the
    session event union), from exactly these fields, checked first. What the
    real client would hand our code for the same JSON."""
    data = as_data(fields)
    require(data, tp, f"a fake {_name(tp)}", response=True)
    return construct_type(type_=tp, value=data)


_SAMPLE_TIME = "2026-10-01T09:00:00Z"


def _sample(tp, key: str = ""):
    """A valid placeholder for a field a fake did not bother to set."""
    tp = _strip(tp)
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is typing.Union or origin is UnionType:
        arms = [_strip(a) for a in args if a is not type(None)]
        return _sample(arms[0], key) if arms else None
    if origin is typing.Literal:
        return args[0]
    if _is_model(tp):
        fields, required = _model_fields(tp)
        return {k: _sample(fields[k], k) for k in required}
    if origin in (list, collections.abc.Sequence, collections.abc.Iterable):
        return []
    if origin in (dict, collections.abc.Mapping):
        return {}
    if tp is bool:
        return False
    if tp is int or tp is float:
        return 0
    if tp is datetime or tp is date:
        return _SAMPLE_TIME
    if tp is str:
        return f"sample_{key}" if key else "sample"
    return None


def _fill(data, tp):
    """`data` with every required field it lacks filled with a placeholder,
    recursively, choosing a union's arm by the `type` the data names. What a
    fake sets is kept as it is, and is still checked afterwards."""
    tp = _strip(tp)
    origin = typing.get_origin(tp)
    if origin is typing.Union or origin is UnionType:
        arms = [_strip(a) for a in typing.get_args(tp) if a is not type(None)]
        models = [a for a in arms if _is_model(a)]
        if isinstance(data, dict) and models:
            kind = data.get("type")
            chosen = [a for a in models if kind in _type_literal(a)] if kind else models[:1]
            if len(chosen) == 1 or (not kind and len(models) == 1):
                return _fill(data, chosen[0])
        return data
    if _is_model(tp) and isinstance(data, dict):
        fields, required = _model_fields(tp)
        out = dict(data)
        for key in required - set(out):
            out[key] = _sample(fields[key], key)
        for key, value in out.items():
            if key in fields:
                out[key] = _fill(value, fields[key])
        return out
    if origin in (list, collections.abc.Sequence, collections.abc.Iterable) and isinstance(data, list):
        item = typing.get_args(tp)[0] if typing.get_args(tp) else typing.Any
        return [_fill(v, item) for v in data]
    return data


# --------------------------------------------------------------------------
# The strict client
# --------------------------------------------------------------------------


@functools.cache
def _real_client():
    # Building a client and walking its resources makes no request.
    return anthropic.Anthropic(api_key="sk-contract", base_url="http://127.0.0.1:9")


@functools.cache
def _resource_class(path: tuple[str, ...]):
    """The SDK class at `client.<path>`: each step a cached_property holding
    a resource."""
    obj = _real_client()
    for name in path:
        attr = inspect.getattr_static(type(obj), name, None)
        if not isinstance(attr, functools.cached_property):
            raise ContractError(f"the SDK has no resource client.{'.'.join(path)}")
        obj = getattr(obj, name)
    return type(obj)


def _method(path: tuple[str, ...]):
    cls = _resource_class(path[:-1])
    func = inspect.getattr_static(cls, path[-1], None)
    if func is None or not callable(func):
        return None
    return func


def _is_resource(path: tuple[str, ...]) -> bool:
    try:
        _resource_class(path)
        return True
    except ContractError:
        return False


def check_request(path: tuple[str, ...], args: tuple, kwargs: dict) -> None:
    func = _method(path)
    where = "client." + ".".join(path)
    if func is None:
        raise ContractError(f"{where}: the SDK has no such method")
    signature = inspect.signature(func)
    try:
        bound = signature.bind(None, *args, **kwargs)
    except TypeError as e:
        raise ContractError(f"{where}{_short(args, kwargs)}: {e}") from None
    hints = typing.get_type_hints(func, include_extras=True)
    errors: list[str] = []
    for name, value in bound.arguments.items():
        if name == "self" or name not in hints:
            continue
        if name in ("extra_headers", "extra_query", "extra_body", "timeout"):
            continue
        errors += check(value, hints[name], name)
    if not errors:
        rule = RULES.get(".".join(path))
        if rule is not None:
            arguments = {k: v for k, v in bound.arguments.items() if k != "self"}
            arguments.update(arguments.pop("kwargs", {}) or {})
            errors += rule(arguments)
    if errors:
        raise ContractError(f"{where} breaks the API contract:\n  " + "\n  ".join(errors))


# --------------------------------------------------------------------------
# What the types cannot say
# --------------------------------------------------------------------------
#
# Limits and cross-field rules from the SDK's docstrings and the Managed
# Agents documentation (the claude-api skill's shared/managed-agents-*.md).
# Each names where it comes from; none is a guess at undocumented behaviour.

MANAGED_AGENTS_BETA = "managed-agents-2026-04-01"
MEMORY_BETA = "agent-memory-2026-07-22"


def _metadata(meta, where: str, limit: int = 16) -> list[str]:
    errors = []
    if not meta:
        return errors
    if len(meta) > limit:
        errors.append(f"{where}: {len(meta)} pairs; at most {limit}")
    for key, value in meta.items():
        if not 1 <= len(key) <= 64:
            errors.append(f"{where}.{key}: keys are 1-64 characters")
        if value is not None and len(str(value)) > 512:
            errors.append(f"{where}.{key}: values are at most 512 characters")
    return errors


def _session_create(a: dict) -> list[str]:
    errors: list[str] = []
    events = list(a.get("initial_events") or [])
    if len(events) > 50:
        errors.append("initial_events: at most 50 events")
    outcomes = [e for e in events if e.get("type") == "user.define_outcome"]
    if len(outcomes) > 1:
        errors.append("initial_events: more than one user.define_outcome is a 400")
    for e in outcomes:
        iterations = e.get("max_iterations")
        if iterations is not None and not 1 <= iterations <= 20:
            errors.append("user.define_outcome.max_iterations: 1-20 (default 3)")
        rubric = e.get("rubric") or {}
        if rubric.get("type") == "text" and len(rubric.get("content", "")) > 262144:
            errors.append("user.define_outcome.rubric.content: at most 262144 characters")
    budget = a.get("budget")
    if budget:
        amount = str(budget["max_list_cost"]["amount"])
        # Minor units as an integer string, > 0, no leading zeros
        # (managed-agents-core.md, Session budgets).
        if not amount.isdigit() or amount.startswith("0"):
            errors.append(f"budget.max_list_cost.amount: {amount!r} must be an integer "
                          "string of cents, > 0, with no leading zeros")
    errors += _metadata(a.get("metadata"), "metadata")
    stores = [r for r in a.get("resources") or [] if r.get("type") == "memory_store"]
    if len(stores) > 8:
        errors.append("resources: at most 8 memory stores per session")
    for i, r in enumerate(a.get("resources") or []):
        if r.get("type") == "file" and not str(r.get("mount_path") or "/").startswith("/"):
            errors.append(f"resources[{i}].mount_path: must be absolute")
        if r.get("type") == "memory_store" and len(r.get("instructions") or "") > 4096:
            errors.append(f"resources[{i}].instructions: at most 4096 characters")
    agent = a.get("agent")
    if isinstance(agent, dict) and agent.get("type") == "agent_with_overrides":
        if "model" in agent and agent["model"] is None:
            errors.append("agent.model: cannot be cleared (agent_model_required)")
        errors += _mcp_references(agent.get("mcp_servers"), agent.get("tools"), "agent")
    return errors


def _mcp_references(servers, tools, where: str) -> list[str]:
    """Every MCP server must be referenced by an mcp_toolset in `tools`, and
    every mcp_toolset must name a server (agent_create_params.py). Either
    list may be None (an override that leaves it as the agent has it), and
    then only the names are checked."""
    errors: list[str] = []
    names = [s.get("name") for s in servers or []]
    if len(names) != len(set(names)):
        errors.append(f"{where}.mcp_servers: names must be unique")
    if len(names) > 20:
        errors.append(f"{where}.mcp_servers: at most 20")
    if tools is None or servers is None:
        return errors
    used = {t.get("mcp_server_name") for t in tools if t.get("type") == "mcp_toolset"}
    for name in names:
        if name not in used:
            errors.append(f"{where}.mcp_servers: {name!r} is not referenced by an mcp_toolset "
                          "in tools; unreferenced servers are rejected")
    for name in used - set(names):
        errors.append(f"{where}.tools: an mcp_toolset names {name!r}, which is not in "
                      "mcp_servers")
    return errors


def _agent(a: dict, *, update: bool) -> list[str]:
    errors: list[str] = []
    name = a.get("name")
    if name is not None and not 1 <= len(name) <= 256:
        errors.append("name: 1-256 characters")
    if len(a.get("system") or "") > 100_000:
        errors.append("system: at most 100,000 characters")
    if len(a.get("description") or "") > 2048:
        errors.append("description: at most 2048 characters")
    if len(list(a.get("skills") or [])) > 20:
        errors.append("skills: at most 20")
    tools = a.get("tools")
    if tools is not None and len(list(tools)) > 128:
        errors.append("tools: at most 128")
    model = a.get("model")
    if isinstance(model, dict) and model.get("inference_geo") not in (None, "us", "global"):
        errors.append(f"model.inference_geo: {model['inference_geo']!r} is not 'us' or 'global'")
    import re

    for t in tools or []:
        if t.get("type") == "custom" and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", t["name"]):
            errors.append(f"tools: custom tool name {t['name']!r} is not 1-128 of "
                          "letters, digits, _ and -")
    errors += _metadata(a.get("metadata"), "metadata")
    # Omitted array fields are preserved and given ones replaced wholesale
    # (managed-agents-core.md, Versioning). Replacing `tools` while leaving
    # `mcp_servers` out keeps the stored servers against the new tools, which
    # the API refuses when one is left unreferenced. The stored servers are
    # not in the request, so this is refused whenever it could happen.
    if update and tools is not None and "mcp_servers" not in a:
        errors.append("tools is replaced but mcp_servers is omitted, so the agent's stored "
                      "servers are kept against the new tools: send mcp_servers too")
    errors += _mcp_references(a.get("mcp_servers") or [], tools or [], "agent")
    return errors


def _memory_beta(a: dict) -> list[str]:
    betas = a.get("betas") or []
    if MANAGED_AGENTS_BETA in betas:
        return [(f"betas: {MANAGED_AGENTS_BETA} on a memory store call is a 400 (the SDK "
                 f"sends {MEMORY_BETA})")]
    return []


def _memory_store_create(a: dict) -> list[str]:
    import unicodedata

    errors = _memory_beta(a)
    name = a.get("name") or ""
    if not 1 <= len(name) <= 255 or any(unicodedata.category(c) == "Cc" for c in name):
        errors.append("name: 1-255 characters, no control characters")
    if len(a.get("description") or "") > 1024:
        errors.append("description: at most 1024 characters")
    return errors + _metadata(a.get("metadata"), "metadata")


def _memory_path(path) -> list[str]:
    if path is None:
        return []
    segments = path.split("/")[1:]
    if (not path.startswith("/") or len(path.encode()) > 1024 or not segments
            or any(s in ("", ".", "..") for s in segments)):
        return [(f"path: {path!r} must start with /, have no empty, . or .. segments, and "
                 "be at most 1024 bytes")]
    return []


def _memory_write(a: dict) -> list[str]:
    errors = _memory_beta(a) + _memory_path(a.get("path"))
    content = a.get("content")
    if content is not None and len(content.encode()) > 102_400:
        errors.append("content: at most 100 kB")
    return errors


def _files_list(a: dict) -> list[str]:
    # environments.md, Session outputs: filtering by scope_id needs the
    # managed-agents header, which client.beta.files does not add.
    scope = a.get("scope_id")
    if isinstance(scope, str) and MANAGED_AGENTS_BETA not in (a.get("betas") or []):
        return [(f"scope_id needs betas=[{MANAGED_AGENTS_BETA!r}]; client.beta.files does "
                 "not send it")]
    if isinstance(scope, str) and not scope.startswith("sesn_"):
        return [f"scope_id: {scope!r} is not a session id (the API validates the prefix)"]
    return []


def _vault_create(a: dict) -> list[str]:
    errors = []
    if not 1 <= len(a.get("display_name") or "") <= 255:
        errors.append("display_name: 1-255 characters")
    return errors + _metadata(a.get("metadata"), "metadata")


def _skill_files(a: dict) -> list[str]:
    """skill_create_params.py: all files in one top-level directory, with a
    SKILL.md at its root."""
    names = []
    for f in a.get("files") or []:
        name = f[0] if isinstance(f, tuple | list) else getattr(f, "name", "")
        names.append(str(name))
    tops = {n.split("/", 1)[0] for n in names}
    errors = []
    if len(tops) != 1 or any("/" not in n for n in names):
        errors.append(f"files: must all sit in one top-level directory, got {sorted(tops)}")
    elif f"{next(iter(tops))}/SKILL.md" not in names:
        errors.append("files: no SKILL.md at the root of the skill's directory")
    if a.get("display_name") is not None and len(a["display_name"]) > 255:
        errors.append("display_name: at most 255 characters")
    return errors


RULES = {
    "beta.sessions.create": _session_create,
    "beta.agents.create": lambda a: _agent(a, update=False),
    "beta.agents.update": lambda a: _agent(a, update=True),
    "beta.memory_stores.create": _memory_store_create,
    "beta.memory_stores.memories.create": _memory_write,
    "beta.memory_stores.memories.update": _memory_write,
    "beta.memory_stores.memories.list": _memory_beta,
    "beta.memory_stores.memories.delete": _memory_beta,
    "beta.files.list": _files_list,
    "beta.vaults.create": _vault_create,
    "skills.create": _skill_files,
    "skills.versions.create": _skill_files,
}


def _short(args, kwargs) -> str:
    return "(" + ", ".join([*(repr(a)[:30] for a in args),
                           *(f"{k}=..." for k in kwargs)]) + ")"


def _return_type(path: tuple[str, ...]):
    func = _method(path)
    return typing.get_type_hints(func)["return"]


def _items_type(tp):
    """For SyncPageCursor[X] / Stream[X]: X. None for anything else."""
    origin = typing.get_origin(tp) or tp
    name = getattr(origin, "__name__", "")
    if name.startswith(("SyncPage", "SyncCursor", "SyncPageCursor", "Stream")):
        args = typing.get_args(tp)
        if args:
            return args[0]
        meta = getattr(tp, "__pydantic_generic_metadata__", None) or {}
        if meta.get("args"):
            return meta["args"][0]
    return None


def _parse(value, tp, where: str):
    """A fake's answer checked against `tp` and parsed as the client would.

    Required fields the fake left out are filled with placeholders first, so
    a test's fake need only set what the test is about; what it does set
    must exist in the real response and have the real type."""
    data = _fill(as_data(value), tp)
    require(data, tp, where, response=True)
    return construct_type(type_=tp, value=data)


class _Stream:
    """A fake event stream, every event held to the stream's event union."""

    def __init__(self, inner, item_tp, where: str):
        self._inner = inner
        self._item_tp = item_tp
        self._where = where

    def __enter__(self):
        entered = self._inner.__enter__() if hasattr(self._inner, "__enter__") else self._inner
        return _Stream(entered, self._item_tp, self._where)

    def __exit__(self, *exc):
        if hasattr(self._inner, "__exit__"):
            return self._inner.__exit__(*exc)
        return False

    def __iter__(self):
        for item in self._inner:
            yield _parse(item, self._item_tp, f"an event from {self._where}")

    def close(self):
        closer = getattr(self._inner, "close", None)
        if closer is not None:
            closer()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _MessageStream:
    """A fake `messages.stream(...)` manager whose final message is held to
    BetaMessage."""

    def __init__(self, inner, where: str):
        self._inner = inner
        self._where = where

    def __enter__(self):
        entered = self._inner.__enter__() if hasattr(self._inner, "__enter__") else self._inner
        return _MessageStream(entered, self._where)

    def __exit__(self, *exc):
        if hasattr(self._inner, "__exit__"):
            return self._inner.__exit__(*exc)
        return False

    def get_final_message(self):
        from anthropic.types.beta.beta_message import BetaMessage

        return _parse(self._inner.get_final_message(), BetaMessage,
                      f"the final message of {self._where}")

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _message_response(path: tuple[str, ...], value, kwargs: dict):
    from anthropic.types.beta.beta_message import BetaMessage

    where = "client." + ".".join(path)
    if path[-1] == "stream" or kwargs.get("stream"):
        return _MessageStream(value, where)
    if path[-1] == "parse":
        # The parsed object is the SDK's own addition, not on the wire.
        data = {k: v for k, v in as_data(value).items() if k != "parsed_output"}
        require(_fill(data, BetaMessage), BetaMessage, f"the answer to {where}",
                response=True)
        return value
    return _parse(value, BetaMessage, f"the answer to {where}")


def _response(path: tuple[str, ...], value, kwargs: dict | None = None):
    where = "client." + ".".join(path)
    if path[-2:-1] == ("messages",) and path[-1] in ("create", "stream", "parse"):
        return _message_response(path, value, kwargs or {})
    tp = _return_type(path)
    item_tp = _items_type(tp)
    name = getattr(typing.get_origin(tp) or tp, "__name__", "")
    if item_tp is not None and name.startswith("Stream"):
        return _Stream(value, item_tp, where)
    if item_tp is not None:
        # A page: the fake answers with the items; iterating a real page
        # yields them (across pages) the same way.
        items = getattr(value, "data", value)
        return [_parse(item, item_tp, f"an item of {where}") for item in items]
    if _is_model(tp) or (typing.get_origin(tp) in (typing.Union, UnionType)
                         and all(_is_model(_strip(a)) for a in typing.get_args(tp))):
        return _parse(value, tp, f"the answer to {where}")
    # Binary responses, stream managers, parsed messages: returned as given.
    return value


class _Call:
    def __init__(self, target, path):
        self._target = target
        self._path = path

    def __call__(self, *args, **kwargs):
        check_request(self._path, args, kwargs)
        return _response(self._path, self._target(*args, **kwargs), kwargs)


class Strict:
    """`fake` (a namespace tree shaped like `anthropic.Anthropic()`), held to
    the SDK's contract on every call. Attributes that are not part of the SDK
    (a test's own bookkeeping) are refused, so a fake cannot grow an API the
    real client does not have."""

    def __init__(self, fake, path: tuple[str, ...] = ()):
        object.__setattr__(self, "_fake", fake)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str):
        path = (*self._path, name)
        if _is_resource(path):
            return Strict(getattr(self._fake, name), path)
        if _method(path) is not None:
            return _Call(getattr(self._fake, name), path)
        raise ContractError(f"client.{'.'.join(path)}: the SDK has no such resource or method")

    def __setattr__(self, name, value):
        # monkeypatch.setattr(fake.client.beta.vaults, "create", down): lands
        # on the fake, and is still called through the contract.
        setattr(self._fake, name, value)


def strict(fake) -> Strict:
    """The fake client, held to the SDK's contract."""
    return fake if isinstance(fake, Strict) else Strict(fake)


__all__ = ["ContractError", "Strict", "as_data", "check", "check_request", "model", "require",
           "strict"]
