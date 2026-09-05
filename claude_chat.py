#!/usr/bin/env python3
"""
Simple Claude (Anthropic) client with zero external dependencies.
Usage:
  - Set ANTHROPIC_API_KEY in the environment (or CLAUDE_API_KEY).
  - Run: python3 claude_chat.py --model claude-2 "Hello Claude"

This script uses the /v1/complete HTTP endpoint and the simple Human/Assistant prompt schema.
"""
import os
import sys
import json
import argparse
import urllib.request
import urllib.error

API_ENV_VARS = ["ANTHROPIC_API_KEY", "CLAUDE_API_KEY"]
API_URL = "https://api.anthropic.com/v1/complete"


def get_api_key():
    for name in API_ENV_VARS:
        v = os.environ.get(name)
        if v:
            return v
    return None


def build_prompt(messages):
    # messages is a list of dicts with 'role' in ('system','user','assistant') and 'content'
    # We'll map to Anthropic's Human/Assistant pattern. System messages precede with a note.
    parts = []
    for m in messages:
        role = m.get('role', 'user')
        content = m.get('content', '')
        if role == 'system':
            # Insert system guidance as a short prefix
            parts.append(f"System: {content}")
        elif role == 'user':
            parts.append(f"Human: {content}\n\nAssistant:")
        elif role == 'assistant':
            parts.append(f"Assistant: {content}\n\nHuman:")
        else:
            parts.append(f"Human: {content}\n\nAssistant:")
    # Join; if last part ends with "Assistant:" we want a final empty Assistant: prompt
    prompt = "\n".join(parts)
    # Ensure prompt ends with Assistant:
    if not prompt.strip().endswith("Assistant:"):
        prompt = prompt + "\n\nAssistant:" 
    return prompt


def complete(api_key, prompt, model="claude-2", max_tokens=512, temperature=0.0):
    headers = {
        'Content-Type': 'application/json',
        'x-api-key': api_key,
        'Accept': 'application/json',
    }
    data = {
        'model': model,
        'prompt': prompt,
        'max_tokens_to_sample': max_tokens,
        'temperature': temperature,
    }
    req = urllib.request.Request(API_URL, data=json.dumps(data).encode('utf-8'), headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            b = resp.read()
            try:
                obj = json.loads(b.decode('utf-8'))
            except Exception:
                print('Non-JSON response from Anthropic:')
                print(b.decode('utf-8', errors='replace'))
                return None
            return obj
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        print(f'HTTP Error {e.code}: {e.reason}', file=sys.stderr)
        print('Response body:', file=sys.stderr)
        print(body, file=sys.stderr)
        return None
    except Exception as e:
        print('Request failed:', e, file=sys.stderr)
        return None


def main(argv=None):
    p = argparse.ArgumentParser(description='Simple Claude client (no external deps)')
    p.add_argument('messages', nargs='*', help='Message text(s). If multiple provided they will be sent as successive user messages.')
    p.add_argument('--model', default='claude-2', help='Model to call (default claude-2)')
    p.add_argument('--max-tokens', type=int, default=512)
    p.add_argument('--temperature', type=float, default=0.0)
    args = p.parse_args(argv)

    api_key = get_api_key()
    if not api_key:
        print('ERROR: No Anthropic API key found. Set one of:', ', '.join(API_ENV_VARS))
        print('You can export it like: export ANTHROPIC_API_KEY=sk-...')
        sys.exit(2)

    if not args.messages:
        print('No messages provided. Example:')
        print('  python3 claude_chat.py "Hello Claude, summarize this:"')
        sys.exit(2)

    # Build messages list from provided strings (all as user messages)
    messages = [{'role': 'user', 'content': m} for m in args.messages]
    prompt = build_prompt(messages)

    obj = complete(api_key, prompt, model=args.model, max_tokens=args.max_tokens, temperature=args.temperature)
    if obj is None:
        print('No response (see errors above).')
        sys.exit(1)

    # Print the main text from the response. The exact field depends on the API version.
    # Try common patterns.
    if 'completion' in obj and isinstance(obj['completion'], str):
        print(obj['completion'].strip())
    elif 'id' in obj and 'completion' not in obj:
        # sometimes anthropic returns 'completion' nested under 'choices'
        choices = obj.get('choices') or []
        if choices and isinstance(choices, list):
            text = choices[0].get('text') or choices[0].get('message') or ''
            print(text.strip())
        else:
            print(json.dumps(obj, indent=2))
    else:
        print(json.dumps(obj, indent=2))


if __name__ == '__main__':
    main()
