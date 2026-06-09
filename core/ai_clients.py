from dataclasses import dataclass

from .models import AIProviderKey


class AIProviderError(Exception):
    pass


@dataclass
class AIMessage:
    role: str
    content: str


@dataclass
class AITextResponse:
    content: str


def provider_error_message(provider_name, exc):
    status_code = getattr(exc, "status_code", None)
    error_code = getattr(exc, "code", None)
    if status_code in {401, 403} or error_code in {"invalid_api_key", "authentication_error"}:
        return f"{provider_name} authentication failed. Check the saved API key."
    return f"{provider_name} request failed: {exc}"


DEFAULT_ANTHROPIC_MAX_TOKENS = 8192


def generate_text(provider_key, messages, system_prompt="", max_tokens=None, model_name=""):
    api_key = provider_key.get_api_key()
    model = model_name or provider_key.model_name
    if provider_key.provider == AIProviderKey.Provider.OPENAI:
        return generate_openai_text(model, api_key, messages, system_prompt, max_tokens)
    if provider_key.provider == AIProviderKey.Provider.ANTHROPIC:
        return generate_anthropic_text(model, api_key, messages, system_prompt, max_tokens)
    if provider_key.provider == AIProviderKey.Provider.GEMINI:
        return generate_gemini_text(model, api_key, messages, system_prompt, max_tokens)
    raise AIProviderError("Unsupported AI provider.")


def stream_text(provider_key, messages, system_prompt="", max_tokens=None, model_name=""):
    api_key = provider_key.get_api_key()
    model = model_name or provider_key.model_name
    if provider_key.provider == AIProviderKey.Provider.OPENAI:
        yield from stream_openai_text(model, api_key, messages, system_prompt, max_tokens)
        return
    if provider_key.provider == AIProviderKey.Provider.ANTHROPIC:
        yield from stream_anthropic_text(model, api_key, messages, system_prompt, max_tokens)
        return
    if provider_key.provider == AIProviderKey.Provider.GEMINI:
        yield from stream_gemini_text(model, api_key, messages, system_prompt, max_tokens)
        return
    raise AIProviderError("Unsupported AI provider.")


def generate_openai_text(model, api_key, messages, system_prompt, max_tokens):
    try:
        from openai import OpenAI, OpenAIError
    except ImportError as exc:
        raise AIProviderError("The OpenAI SDK is not installed.") from exc

    try:
        client = OpenAI(api_key=api_key)
        api_messages = []
        if system_prompt:
            api_messages.append({"role": "system", "content": system_prompt})
        api_messages.extend(
            {"role": message.role, "content": message.content} for message in messages
        )
        request_kwargs = {
            "model": model,
            "messages": api_messages,
        }
        if max_tokens is not None:
            request_kwargs["max_completion_tokens"] = max_tokens
        response = client.chat.completions.create(
            **request_kwargs,
        )
        return AITextResponse(response.choices[0].message.content or "")
    except OpenAIError as exc:
        raise AIProviderError(provider_error_message("OpenAI", exc)) from exc


def stream_openai_text(model, api_key, messages, system_prompt, max_tokens):
    try:
        from openai import OpenAI, OpenAIError
    except ImportError as exc:
        raise AIProviderError("The OpenAI SDK is not installed.") from exc

    try:
        client = OpenAI(api_key=api_key)
        api_messages = []
        if system_prompt:
            api_messages.append({"role": "system", "content": system_prompt})
        api_messages.extend(
            {"role": message.role, "content": message.content} for message in messages
        )
        request_kwargs = {
            "model": model,
            "messages": api_messages,
            "stream": True,
        }
        if max_tokens is not None:
            request_kwargs["max_completion_tokens"] = max_tokens
        stream = client.chat.completions.create(**request_kwargs)
        for chunk in stream:
            content = chunk.choices[0].delta.content
            if content:
                yield content
    except OpenAIError as exc:
        raise AIProviderError(provider_error_message("OpenAI", exc)) from exc


def generate_anthropic_text(model, api_key, messages, system_prompt, max_tokens):
    try:
        from anthropic import Anthropic, AnthropicError
    except ImportError as exc:
        raise AIProviderError("The Anthropic SDK is not installed.") from exc

    try:
        client = Anthropic(api_key=api_key)
        api_messages = [
            {"role": message.role, "content": message.content}
            for message in messages
            if message.role in {"user", "assistant"}
        ]
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens or DEFAULT_ANTHROPIC_MAX_TOKENS,
            system=system_prompt or None,
            messages=api_messages,
        )
        text_parts = [
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ]
        return AITextResponse("\n".join(text_parts))
    except AnthropicError as exc:
        raise AIProviderError(provider_error_message("Anthropic", exc)) from exc


def stream_anthropic_text(model, api_key, messages, system_prompt, max_tokens):
    try:
        from anthropic import Anthropic, AnthropicError
    except ImportError as exc:
        raise AIProviderError("The Anthropic SDK is not installed.") from exc

    try:
        client = Anthropic(api_key=api_key)
        api_messages = [
            {"role": message.role, "content": message.content}
            for message in messages
            if message.role in {"user", "assistant"}
        ]
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens or DEFAULT_ANTHROPIC_MAX_TOKENS,
            system=system_prompt or None,
            messages=api_messages,
        ) as stream:
            for text in stream.text_stream:
                if text:
                    yield text
    except AnthropicError as exc:
        raise AIProviderError(provider_error_message("Anthropic", exc)) from exc


def generate_gemini_text(model, api_key, messages, system_prompt, max_tokens):
    try:
        from google import genai
    except ImportError as exc:
        raise AIProviderError("The Google Gen AI SDK is not installed.") from exc

    client = genai.Client(api_key=api_key)
    contents = "\n\n".join(
        f"{message.role.upper()}:\n{message.content}" for message in messages
    )
    try:
        config = {"system_instruction": system_prompt}
        if max_tokens is not None:
            config["max_output_tokens"] = max_tokens
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        return AITextResponse(getattr(response, "text", "") or "")
    except Exception as exc:
        raise AIProviderError(provider_error_message("Gemini", exc)) from exc


def stream_gemini_text(model, api_key, messages, system_prompt, max_tokens):
    try:
        from google import genai
    except ImportError as exc:
        raise AIProviderError("The Google Gen AI SDK is not installed.") from exc

    try:
        client = genai.Client(api_key=api_key)
        contents = "\n\n".join(
            f"{message.role.upper()}:\n{message.content}" for message in messages
        )
        config = {"system_instruction": system_prompt}
        if max_tokens is not None:
            config["max_output_tokens"] = max_tokens
        stream = client.models.generate_content_stream(
            model=model,
            contents=contents,
            config=config,
        )
        for chunk in stream:
            text = getattr(chunk, "text", "") or ""
            if text:
                yield text
    except Exception as exc:
        raise AIProviderError(provider_error_message("Gemini", exc)) from exc
