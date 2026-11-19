"""One API call per provider, with errors sorted into three kinds.

Fatal      stop the whole provider (bad key, out of credit)
CellError  skip this model or model+effort (unknown model, unsupported effort)
Transient  wait and try again (rate limit, server error, network)

Replies are free text. A forced JSON format was tried and dropped: with it,
Claude skipped thinking entirely on this question (0 thinking tokens in 566 of
566 calls, against thinking on both calls without it), which made reasoning
effort meaningless, and GPT spent its reasoning on the format. Every reply is
instead checked afterwards to be a number and nothing else (parse_answer).

Reasoning: neither API returns the raw chain of thought. What is saved is the
reasoning summary each API provides, plus the reasoning token count.
"""
import json
import random
import re
import time
import urllib.error
import urllib.request

class Fatal(Exception):
    pass


class CellError(Exception):
    def __init__(self, scope, message):
        super().__init__(message)
        self.scope = scope  # "model" or "effort"


class Transient(Exception):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def _short(err):
    return re.sub(r"\s+", " ", str(getattr(err, "message", None) or err))[:220]


def _retry_after(err):
    try:
        return float(err.response.headers.get("retry-after"))
    except Exception:
        return None


def _usage_dict(usage):
    try:
        return usage.to_dict()
    except Exception:
        return None


# ---------------------------------------------------------------- OpenAI

_NO_SUMMARY = set()  # models where this key may not request reasoning summaries


def make_openai(api_key):
    import openai
    return openai.OpenAI(api_key=api_key, max_retries=0, timeout=180.0)


def call_openai(client, model, effort, prompt, max_out):
    import openai
    reasoning = {"effort": effort}
    if model not in _NO_SUMMARY:
        reasoning["summary"] = "auto"
    try:
        r = client.responses.create(
            model=model,
            input=prompt,
            reasoning=reasoning,
            max_output_tokens=max_out,
            store=False,
        )
    except openai.AuthenticationError as e:
        raise Fatal("API key rejected: " + _short(e))
    except openai.PermissionDeniedError as e:
        raise CellError("model", _short(e))
    except openai.NotFoundError as e:
        raise CellError("model", _short(e))
    except openai.RateLimitError as e:
        if "insufficient_quota" in str(e):
            raise Fatal("out of credit: " + _short(e))
        raise Transient(_short(e), _retry_after(e))
    except openai.BadRequestError as e:
        low = str(e).lower()
        if "summary" in reasoning and ("summar" in low or "verif" in low):
            _NO_SUMMARY.add(model)  # e.g. organisation not verified for summaries: run without
            return call_openai(client, model, effort, prompt, max_out)
        raise CellError("effort", _short(e))
    except openai.APIStatusError as e:
        if e.status_code >= 500 or e.status_code in (408, 409):
            raise Transient(_short(e), _retry_after(e))
        raise CellError("effort", _short(e))
    except openai.APIConnectionError as e:
        raise Transient("network: " + _short(e))

    parts = []
    for item in r.output or []:
        if getattr(item, "type", "") == "reasoning":
            raw = getattr(item, "content", None) or []
            parts += [c.text for c in raw] if raw else [s.text for s in (item.summary or [])]
    usage = r.usage
    details = getattr(usage, "output_tokens_details", None)
    stop = r.status or ""
    if stop != "completed" and getattr(r, "incomplete_details", None):
        stop += ":" + str(getattr(r.incomplete_details, "reason", ""))
    return {
        "text": r.output_text or "",
        "reasoning": "\n\n".join(parts),
        "reasoning_note": "reasoning summary not available for this model on this API key" if model in _NO_SUMMARY else None,
        "in_tok": usage.input_tokens,
        "out_tok": usage.output_tokens,
        "reason_tok": getattr(details, "reasoning_tokens", None),
        "usage": _usage_dict(usage),
        "served_model": r.model,
        "stop": stop,
    }


# ---------------------------------------------------------------- Anthropic

def make_anthropic(api_key):
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=180.0)


def call_anthropic(client, model, effort, prompt, max_out):
    import anthropic
    try:
        # Adaptive thinking is each model's default; display="summarized" only makes
        # the thinking readable in the response, it does not change how much is done.
        # Effort is the only depth control. No temperature: non-default sampling is
        # rejected. No refusal fallback: a fallback would answer from a different
        # model and contaminate the cell.
        r = client.messages.create(
            model=model,
            max_tokens=max_out,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": effort},
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as e:
        raise Fatal("API key rejected: " + _short(e))
    except anthropic.PermissionDeniedError as e:
        raise CellError("model", _short(e))
    except anthropic.NotFoundError as e:
        raise CellError("model", _short(e))
    except anthropic.RateLimitError as e:
        raise Transient(_short(e), _retry_after(e))
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e).lower():
            raise Fatal("out of credit: " + _short(e))
        raise CellError("effort", _short(e))
    except anthropic.APIStatusError as e:
        if e.status_code >= 500 or e.status_code in (408, 409):
            raise Transient(_short(e), _retry_after(e))
        raise CellError("effort", _short(e))
    except anthropic.APIConnectionError as e:
        raise Transient("network: " + _short(e))

    details = getattr(r.usage, "output_tokens_details", None)
    return {
        "text": "".join(b.text for b in r.content if b.type == "text"),
        "reasoning": "\n\n".join(b.thinking for b in r.content if b.type == "thinking"),
        "reasoning_note": None,
        "in_tok": r.usage.input_tokens,
        "out_tok": r.usage.output_tokens,  # includes thinking tokens
        "reason_tok": getattr(details, "thinking_tokens", None),
        "usage": _usage_dict(r.usage),
        "served_model": r.model,
        "stop": r.stop_reason or "",
    }


# ---------------------------------------------------------------- Decision APIs
#
# A decision model is not asked to write a reply. It is given the question and a
# fixed list of options, and returns one option plus a probability for every
# option. There is no reasoning effort and no sampling setting. What can be
# varied is the order the options are listed in, which both vendors say can
# move the answer, so the order is the condition these tests vary.

def decision_options(k, order, rng):
    """The numbers 1..k as option names, in the order this call lists them."""
    options = [str(n) for n in range(1, k + 1)]
    if order == "descending":
        options.reverse()
    elif order == "shuffled":
        rng.shuffle(options)
    return options


def _decision_result(prompt, state, options, choice, probs, confidence, usage, served, stop):
    """Common shape for both decision APIs. `probs` maps option name -> probability."""
    missing = [o for o in options if o not in probs]
    return {
        "text": "" if choice is None else str(choice),
        "input": json.dumps({"input": state, "question": prompt, "options": options}, ensure_ascii=False),
        "reasoning": None,
        "reasoning_note": "decision model: returns probabilities, no reasoning text",
        "in_tok": int((usage or {}).get("input_tokens") or 0),
        "out_tok": int((usage or {}).get("output_tokens") or 0),
        "reason_tok": None,
        "usage": usage,
        "served_model": served,
        "stop": stop,
        "options": options,
        "probs": {o: float(probs.get(o, 0.0)) for o in options},
        "confidence": confidence,
        # an option left out of the returned distribution is counted as 0 and the call is flagged
        "flag": "missing_probabilities" if missing and choice is not None else None,
    }


def call_openai_decision(client, model, prompt, options, state=""):
    """POST /v1/decisions with one choice question. Choice values must be strings."""
    import openai
    try:
        r = client.decisions.create(
            model=model,
            input=state,
            questions=[{"type": "choice", "name": "number", "instructions": prompt,
                        "choices": [{"value": o} for o in options]}],
        )
    except openai.AuthenticationError as e:
        raise Fatal("API key rejected: " + _short(e))
    except openai.PermissionDeniedError as e:
        raise CellError("model", _short(e))
    except openai.NotFoundError as e:
        raise CellError("model", _short(e))
    except openai.RateLimitError as e:
        if "insufficient_quota" in str(e):
            raise Fatal("out of credit: " + _short(e))
        raise Transient(_short(e), _retry_after(e))
    except openai.BadRequestError as e:
        raise CellError("model", _short(e))
    except openai.APIStatusError as e:
        if e.status_code >= 500 or e.status_code in (408, 409):
            raise Transient(_short(e), _retry_after(e))
        raise CellError("model", _short(e))
    except openai.APIConnectionError as e:
        raise Transient("network: " + _short(e))

    usage = _usage_dict(r.usage)
    answer = r.answers[0] if r.answers else None
    if answer is None or getattr(answer, "type", "") != "choice":
        return _decision_result(prompt, state, options, None, {}, None, usage, r.model, "refusal")
    probs = {str(p.value): p.probability for p in (answer.probabilities or [])}
    return _decision_result(prompt, state, options, answer.choice, probs, answer.confidence, usage, r.model, "completed")


JEV_URL = "https://api.typesafe.ai/v1/systemone"


def make_jev(api_key):
    return {"key": api_key}


def call_jev(client, model, prompt, options, state=""):
    """POST /v1/systemone with one Choice question (plain HTTPS, no SDK needed).

    The options go in `criteria` as a map of option name -> description; the
    descriptions are left empty (null) because the option names are the numbers."""
    body = json.dumps({
        "model": model,
        "state": state,
        "questions": {"number": {"type": "choice", "instructions": prompt, "criteria": {o: None for o in options}}},
    }).encode("utf-8")
    req = urllib.request.Request(JEV_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + client["key"], "Content-Type": "application/json",
        "User-Agent": "llm-random-number-benchmark",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        msg = _short("HTTP %d %s" % (e.code, detail))
        if e.code in (401, 402):
            raise Fatal(("API key rejected: " if e.code == 401 else "out of credit: ") + msg)
        if e.code in (408, 409, 429, 529) or e.code >= 500:
            try:
                wait = float(e.headers.get("retry-after"))
            except (TypeError, ValueError):
                wait = None
            raise Transient(msg, wait)
        raise CellError("model", msg)      # 400, 403, 404, 422: the request itself is not accepted
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Transient("network: " + _short(e))
    except ValueError as e:
        raise Transient("unreadable reply: " + _short(e))

    answer = (data.get("answers") or {}).get("number") or {}
    usage = data.get("usage")
    if answer.get("type") != "choice" or answer.get("choice") is None:
        return _decision_result(prompt, state, options, None, {}, None, usage, data.get("model", model), "refusal")
    probs = {str(name): p for name, p in (answer.get("probabilities") or {}).items()}
    return _decision_result(prompt, state, options, answer["choice"], probs, answer.get("confidence"),
                            usage, data.get("model", model), "completed")


def call_demo_decision(family, model, order, k, rng=random):
    """Invented decision answers so the UI can be tried without API keys. Not model data."""
    time.sleep(rng.uniform(0.02, 0.08))
    options = decision_options(k, order, rng)
    favourite = 7 if k >= 7 else k
    weight = {}
    for pos, o in enumerate(options):
        n = int(o)
        w = 1.0 + (9.0 if n == favourite else 2.0 if abs(n - favourite) == 3 else 0.0)
        w *= 1.0 + (1.5 if pos == 0 else 0.0)            # a lean toward the first option listed
        weight[o] = w * rng.uniform(0.85, 1.15)
    total = sum(weight.values())
    probs = {o: w / total for o, w in weight.items()}
    if family == "TypeSafe":                              # one of the two always picks its top option
        choice = max(probs, key=probs.get)
    else:
        choice = rng.choices(list(probs), weights=list(probs.values()))[0]
    top = sorted(probs.values(), reverse=True)
    usage = {"input_tokens": 60, "output_tokens": 0}
    return _decision_result("(simulated)", "", options, choice, probs, round(top[0] - top[1], 3), usage, model, "demo")


# ---------------------------------------------------------------- Demo (simulated)

_DEMO_FAVOURITES = {
    "GPT": {10: [7, 7, 7, 3, 4, 8], 100: [42, 42, 37, 37, 73, 57, 47], 1000: [427, 742, 387, 437, 573, 847]},
    "Claude": {10: [7, 7, 7, 7, 4, 6], 100: [47, 47, 42, 73, 73, 37, 67], 1000: [742, 847, 472, 743, 637, 384]},
}
_DEMO_STICKINESS = {"low": 0.85, "medium": 0.72, "high": 0.55}


def call_demo(family, model, effort, k, rng=random):
    """Invented answers so the UI can be tried without API keys. Not model data."""
    time.sleep(rng.uniform(0.04, 0.16))
    favourites = _DEMO_FAVOURITES.get(family, {}).get(k)
    if favourites and rng.random() < _DEMO_STICKINESS.get(effort, 0.7):
        n = rng.choice(favourites)
        if rng.random() < 0.25:
            n = min(k, max(1, n + rng.choice([-2, -1, 1, 2]) * (1 if k <= 100 else rng.randint(1, 9))))
    else:
        n = rng.randint(1, k)
    thinking = int({"low": 20, "medium": 90, "high": 260}.get(effort, 60) * rng.uniform(0.5, 1.5))
    roll = rng.random()   # a few imperfect replies, to exercise the quality check
    text = "**%d**" % n if roll < 0.004 else "I can't pick truly at random." if roll < 0.006 else str(n)
    return {
        "text": text,
        "reasoning": "(simulated: the demo makes no API call, so there is no reasoning)",
        "reasoning_note": None,
        "in_tok": 22,
        "out_tok": 6 + thinking,
        "reason_tok": thinking,
        "usage": {"input_tokens": 22, "output_tokens": 6 + thinking},
        "served_model": model,
        "stop": "demo",
    }


# ---------------------------------------------------------------- Parsing

_NUM = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


def parse_number(text, k):
    """Free-text fallback: valid only if exactly one integer in 1..k was given."""
    t = (text or "").strip()
    if not t:
        return None, "empty"
    tokens = set(_NUM.findall(t))
    if not tokens:
        return None, "no_number"
    if len(tokens) > 1:
        return None, "ambiguous"
    token = tokens.pop().replace(",", "")
    if "." in token:
        return None, "not_integer"
    n = int(token)
    if n < 1 or n > k:
        return None, "out_of_range"
    return n, "ok"


_NUMBER_ONLY = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")


def parse_answer(text, k):
    """Quality check on one reply. Returns (number, reason).

    ok          the reply is a number and nothing else, and it is in 1..k
    extra_text  exactly one in-range integer, but with other characters around it
                (the number is still counted, and the reply is flagged)
    anything else: no usable number, the reply is left out of the distributions
    """
    t = (text or "").strip()
    if _NUMBER_ONLY.fullmatch(t):
        n = int(t.replace(",", ""))
        return (n, "ok") if 1 <= n <= k else (None, "out_of_range")
    n, reason = parse_number(t, k)
    return (n, "extra_text") if n is not None else (None, reason)
