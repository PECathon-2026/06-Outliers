
"""LLM router with ordered per-role fallbacks and an offline scripted model."""

import json
import os
import re
import time
import urllib.request
from pathlib import Path

import yaml


# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent / "config.yaml"

if _CONFIG_PATH.exists():
    with _CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        CFG = yaml.safe_load(config_file) or {}
else:
    CFG = {}

CFG.setdefault("limits", {})
CFG.setdefault("roles", {})

CFG["limits"].setdefault("max_iters", 6)
CFG["limits"].setdefault("max_steps", 8)
CFG["limits"].setdefault("same_failure_limit", 2)
CFG["limits"].setdefault("max_review_rounds", 2)
CFG["limits"].setdefault("max_seconds", 300)
CFG["limits"].setdefault("timeout", 30)

BASES = {
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    "unorouter": os.getenv("UNOROUTER_BASE_URL", ""),
}

# Tests and other modules use KEYS.values() to clear API keys.
KEYS = {
    "nvidia": "NVIDIA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "google": "GOOGLE_API_KEY",
    "unorouter": "UNOROUTER_API_KEY",
}


# ------------------------------------------------------------
# HTTP provider client
# ------------------------------------------------------------

def _http(provider, model, system, user):
    if provider not in KEYS or provider not in BASES:
        raise RuntimeError(f"Unknown LLM provider: {provider}")

    key = os.getenv(KEYS[provider])
    base = BASES[provider]

    if not key:
        raise RuntimeError(f"{KEYS[provider]} not set")

    if not base:
        raise RuntimeError(f"Base URL for {provider} is not set")

    body = {
        "model": model,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )

    timeout = CFG["limits"].get("timeout", 30)

    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)

    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Provider returned an unexpected response") from exc


def _json(text):
    """Parse JSON, including JSON wrapped in a Markdown code block."""
    if isinstance(text, dict):
        return text

    if not isinstance(text, str):
        raise ValueError("LLM response was not text")

    text = text.strip()

    # First try parsing the complete response.
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Then try extracting a JSON object from surrounding prose/code fences.
    match = re.search(r"\{.*\}", text, re.DOTALL)

    if not match:
        raise ValueError("LLM response did not contain a JSON object")

    result = json.loads(match.group(0))

    if not isinstance(result, dict):
        raise ValueError("LLM response JSON must be an object")

    return result


# ------------------------------------------------------------
# Offline mock
# ------------------------------------------------------------

def _mock_builder(data):
    """Offline scripted builder for the discount_shop test repository."""

    history = data.get("history", [])
    used = [item.get("tool") for item in history]
    issues = " ".join(str(issue) for issue in data.get("issues", []))
    files = data.get("files", [])

    # Handle the planted hardcoded-secret finding.
    secret_match = re.search(
        r"([\w./-]+\.py):\d+ hardcoded secret",
        issues,
    )

    if secret_match:
        path = secret_match.group(1)

        read_result = next(
            (
                item for item in history
                if item.get("tool") == "read_file"
                and item.get("args", {}).get("path") == path
            ),
            None,
        )

        if read_result is None:
            return {
                "tool": "read_file",
                "args": {"path": path},
                "reason": "Inspect the file flagged by the security scan.",
            }

        if "edit_file" not in used:
            content = read_result.get("result", "")

            assignment = next(
                (
                    line.strip()
                    for line in content.splitlines()
                    if re.match(r"^\s*\w+\s*=\s*[\"']", line)
                ),
                None,
            )

            if assignment:
                name = assignment.split("=", 1)[0].strip()

                return {
                    "tool": "edit_file",
                    "args": {
                        "path": path,
                        "old": assignment,
                        "new": (
                            "import os\n"
                            f'{name} = os.environ.get("{name}", "")'
                        ),
                    },
                    "reason": "Move the hardcoded secret to an environment variable.",
                }

        return {
            "tool": "done",
            "reason": "Finished the scripted secret-remediation attempt.",
        }

    # The offline builder only knows how to implement this sample task.
    if "discount.py" not in files:
        return {
            "tool": "done",
            "reason": (
                "The offline mock does not know this repository or task. "
                "Configure an LLM provider for general-purpose implementation."
            ),
        }

    if not history:
        return {
            "tool": "run_tests",
            "args": {},
            "reason": "Run the tests to observe the current behavior.",
        }

    if len(history) == 1:
        return {
            "tool": "read_file",
            "args": {"path": "discount.py"},
            "reason": "Inspect the discount calculation.",
        }

    if len(history) == 2:
        read_step = next(
            (
                item for item in history
                if item.get("tool") == "read_file"
                and item.get("args", {}).get("path") == "discount.py"
            ),
            None,
        )

        if read_step:
            old_line = next(
                (
                    line.strip()
                    for line in read_step.get("result", "").splitlines()
                    if line.strip().startswith("return")
                ),
                None,
            )

            if old_line:
                return {
                    "tool": "edit_file",
                    "args": {
                        "path": "discount.py",
                        "old": old_line,
                        "new": "return price * (1 - pct / 100)",
                    },
                    "reason": (
                        "Calculate the discounted price using the "
                        "complement of the percentage."
                    ),
                }

    return {
        "tool": "done",
        "reason": "The scripted discount fix has been applied.",
    }


def mock(role, data):
    """Return deterministic offline responses used by tests and demos."""

    if role == "head":
        if data.get("phase") == "triage":
            request = data.get("request", "")

            if len(request.split()) < 3:
                return {
                    "action": "clarify",
                    "question": "Please describe what should change and where.",
                }

            return {
                "action": "proceed",
                "tasks": [
                    "Analyse the requirement",
                    "Implement the fix",
                    "Verify with tests",
                    "Review and security scan",
                ],
            }

        return {
            "action": (
                "change_approach"
                if data.get("esc", 0) == 0
                else "ask_human"
            )
        }

    if role == "planner":
        return {
            "objective": data.get("request", ""),
            "criteria": [
                "Every test in the repository passes",
                "No high-severity security finding remains",
            ],
            "files": list(data.get("files", [])),
            "conventions": "Use the repository's existing coding conventions.",
            "risks": ["Existing behavior must remain unchanged."],
        }

    if role == "builder":
        return _mock_builder(data)

    if role == "verifier":
        return {
            "verdict": (
                "pass" if data.get("test_exit") == 0 else "fail"
            )
        }

    # Reviewer and any other roles.
    return {
        "verdict": "approve",
        "notes": ["Review completed by the offline mock."],
    }


# ------------------------------------------------------------
# Public LLM interface
# ------------------------------------------------------------

def call(role, system, data, emit):
    """
    Call configured providers in order and fall back to the offline mock.

    Returns:
        (response_dict, model_name)
    """

    providers = CFG.get("roles", {}).get(role, [])

    for provider_config in providers:
        provider = provider_config.get("provider")
        model = provider_config.get("model")

        if not provider or not model:
            emit(
                "router",
                "decision",
                {"msg": f"{role}: skipping incomplete provider configuration"},
            )
            continue

        for attempt in range(2):
            try:
                raw = _http(
                    provider,
                    model,
                    system,
                    json.dumps(data, ensure_ascii=False),
                )

                return _json(raw), model

            except RuntimeError as exc:
                # Missing credentials/configuration: don't retry this provider.
                emit(
                    "router",
                    "decision",
                    {
                        "msg": (
                            f"{role}: {provider} unavailable "
                            f"({str(exc)[:100]}), falling back"
                        )
                    },
                )
                break

            except Exception as exc:
                if attempt == 0:
                    time.sleep(1.0)
                else:
                    emit(
                        "router",
                        "decision",
                        {
                            "msg": (
                                f"{role}: {provider} failed twice "
                                f"({str(exc)[:100]}), falling back"
                            )
                        },
                    )

    emit(
        "router",
        "decision",
        {"msg": f"{role}: using offline mock"},
    )

    return mock(role, data), "mock-offline"