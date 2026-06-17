"""Antigravity CLI command-shape helper (binary ``agy``).

The Antigravity CLI is invoked headless as::

    agy --model "<MODEL>" --add-dir "<project_dir>" \
        --dangerously-skip-permissions -p "<PROMPT>"

Unlike claude/codex/gemini/grok, ``agy`` emits **plain text** on stdout — there
is no ``--output-format`` flag, no JSON/JSONL stream, and therefore no per-event
usage block to parse. The dispatch layer relies on the no-parser passthrough:
because ``agy`` is deliberately absent from ``cli_agent._PARSERS``, the read loop
returns raw stdout verbatim and reports zero token usage. This module owns only
the argv shape; there is no usage/session parser here on purpose.

The reasoning effort is baked into the model display-name (e.g.
``"Gemini 3.5 Flash (Low)"``), so there is no ``--effort`` flag. ``agy`` also has
no stdin prompt mode and no prompt-file mode — the prompt is always passed as an
argv element via ``-p``.
"""

from __future__ import annotations


def build_argv(
    *,
    prompt: str,
    binary: str | None,
    model: str | None,
    reasoning_effort: str | None,
    extra_flags: tuple[str, ...],
    project_dir: str | None,
    prompt_on_stdin: bool,
    prompt_file: str | None = None,
) -> list[str]:
    """Return argv for one non-interactive Antigravity (``agy``) invocation.

    Keyword signature mirrors ``cli_grok.build_argv`` so the ``cli_agent``
    dispatch call site stays uniform across CLI agent types. ``reasoning_effort``
    (baked into *model*), ``prompt_on_stdin``, and ``prompt_file`` are accepted
    only for signature parity and are intentionally ignored: ``agy`` has no
    effort flag, no stdin prompt mode, and no prompt-file mode. *model* is the
    display-name string (e.g. ``"Gemini 3.5 Flash (Low)"``). *extra_flags*
    carries ``--dangerously-skip-permissions`` via the YOLO default.
    """
    resolved_binary = binary or "agy"
    args = [resolved_binary]
    if model:
        args += ["--model", model]
    if project_dir:
        args += ["--add-dir", project_dir]
    args.extend(extra_flags)
    args += ["-p", prompt]
    return args


def extract_output(raw: str) -> str:
    """Extract the actual agent output from an agy task-status block.

    When agy uses its async task system it emits:
        [Task <id>/task-N Status Update]
        Status: COMPLETED
        Exit Code: 0
        Log Path: file:///...
        Output:
        <actual model output>
        Error: <message or "(none)">

    ``parse_skill_result`` needs the content of the ``Output:`` section, not
    the wrapper. When the block is absent (streaming mode), returns *raw* unchanged.
    ``(empty)`` output is normalised to an empty string so the caller gets a
    clean ``no valid result block`` error rather than trying to parse "(empty)".
    """
    if "[Task " not in raw or "Status Update]" not in raw:
        return raw

    output_marker = "\nOutput:\n"
    error_marker = "\nError:"
    start = raw.find(output_marker)
    if start == -1:
        return raw
    content_start = start + len(output_marker)
    end = raw.find(error_marker, content_start)
    content = raw[content_start:end].strip() if end != -1 else raw[content_start:].strip()
    return "" if content == "(empty)" else content
