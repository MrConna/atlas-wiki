# Repository instructions with AGENTS.md

Source: https://developers.openai.com/codex/agent-configuration/agents-md

## Instruction discovery

Codex first reads global guidance from its home directory. It then walks from the project root toward the current working directory, selecting at most one instruction file in each directory. An AGENTS.override.md file takes precedence over AGENTS.md in the same location.

## Merge precedence

Instruction files are combined from the root toward the current directory. Guidance located closer to the current directory appears later and overrides broader guidance when the two conflict.

## Fallback files and limits

Repositories can configure alternate instruction filenames. Empty files are skipped, and the combined instruction chain stops growing when it reaches the configured project documentation byte limit.

## Review guidance

Repository-wide code review rules belong near the repository root. Rules that apply only to a service should live in a closer nested instruction file.
