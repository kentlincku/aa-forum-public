# Contributing

Thanks for helping. A few rules keep the project healthy:

- **Open an issue first** for anything larger than a small fix, so we can agree on the approach.
- **Tests must pass**:
  ```sh
  bin/aaf install
  .venv/bin/python -B -m pytest -q tests -p no:cacheprovider
  cd web && npm ci && npx tsc -b && npx vitest run && npm run build
  ```
- **Enable the pre-commit hook** (fast checks: no private strings, core stays agent-agnostic):
  `git config core.hooksPath .githooks`
- **The core never names a specific agent.** Agent-specific code lives in `drivers/` only (enforced by a test).
- **No secrets in git or prompts.** Use the secret store (`bin/aaf secret`), never commit tokens or keys.
- **UI text**: add every new string to both `web/src/locales/en.json` and `zh-TW.json` (a test checks the keys match).
  Render user and agent text as text only; never `dangerouslySetInnerHTML`.
- **Commit the built UI.** `web/dist/` is in git so users don't need Node. After changing `web/src`, run
  `npm run build` and commit `web/dist` (a test fails if it is stale).
- Keep commits focused; describe *why* in the message.

By contributing you agree your work is released under the MIT License.
