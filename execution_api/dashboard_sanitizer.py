"""Dashboard code sanitizer.

Processes LLM-generated Panel code before deployment.
Since the dispatcher now uses `panel serve` directly, most Panel code
works as-is. The sanitizer handles remaining API gotchas.

To add a new rule:
  1. Add the failing LLM code as a test in tests/unit/test_dashboard_sanitizer.py
  2. Add a _rule_* function below
  3. Register it in RULES
"""

import re
from typing import Callable

Rule = Callable[[str], str]


def _rule_rewrite_depends_to_bind(code: str) -> str:
    """Rewrite @pn.depends decorators to pn.bind() calls.

    @pn.depends passes widget values as positional args, which breaks
    when decorated functions call each other. pn.bind() is explicit and safe.
    """
    pattern = re.compile(
        r'@pn\.depends\(([^)]+)\)\s*\n'
        r'def\s+(\w+)\s*\(\s*\*\*\w+\s*\):',
        re.MULTILINE,
    )
    if not pattern.search(code):
        return code

    replacements = []
    for match in pattern.finditer(code):
        decorator_args = match.group(1).strip()
        func_name = match.group(2)
        new_def = f'def {func_name}(**kwargs):'
        replacements.append((match.start(), match.end(), new_def, func_name, decorator_args))

    lines = code
    for start, end, new_def, func_name, decorator_args in reversed(replacements):
        lines = lines[:start] + new_def + lines[end:]

    bind_lines = []
    for _, _, _, func_name, decorator_args in replacements:
        widget_names = [w.strip() for w in decorator_args.split(',')]
        bind_kwargs = ', '.join(f'{w}={w}' for w in widget_names)
        bind_lines.append(f'{func_name}_bound = pn.bind({func_name}, {bind_kwargs})')

    if bind_lines:
        lines = lines.rstrip() + '\n\n' + '\n'.join(bind_lines) + '\n'
        for _, _, _, func_name, _ in replacements:
            lines = _replace_in_layout(lines, func_name, f'{func_name}_bound')

    return lines


def _replace_in_layout(code: str, old_name: str, new_name: str) -> str:
    pattern = re.compile(
        r'(?<![.\w])' + re.escape(old_name) + r'(?!\s*[\w(=])'
        r'(?=\s*[,)\]])'
    )
    return pattern.sub(new_name, code)


def _rule_ensure_servable(code: str) -> str:
    """Ensure the code calls .servable() so panel serve picks it up."""
    if '.servable()' in code:
        return code

    candidates = [
        r'(\w+)\s*=\s*pn\.Column\(',
        r'(\w+)\s*=\s*pn\.Row\(',
        r'(\w+)\s*=\s*pn\.Tabs\(',
        r'(\w+)\s*=\s*pn\.GridSpec\(',
    ]
    last_layout_var = None
    for pat in candidates:
        matches = list(re.finditer(pat, code, re.MULTILINE))
        if matches:
            last_layout_var = matches[-1].group(1)

    if last_layout_var:
        code = code.rstrip() + f'\n\n{last_layout_var}.servable()\n'

    return code


# ── Rule registry ────────────────────────────────────────────────────
# Rules run in order. Add new rules here.
RULES: list[Rule] = [
    _rule_rewrite_depends_to_bind,
    _rule_ensure_servable,
]


def sanitize_dashboard_code(code: str) -> str:
    for rule in RULES:
        code = rule(code)
    return code
