# Contributing to dsh-fleet

Run the loopback Mock fleet when developing UI, routing or file-handling changes. Use a disposable workspace for plugin tests; real Desktop integrations can depend on the installed DSH version. Include the operating system, component, expected behavior and a minimal Mock reproduction when reporting an issue.

## Development checks

From the repository root:

```sh
uv sync --locked
npm --prefix apps/web-pwa ci
npm --prefix apps/web-pwa run test:unit
npm --prefix apps/web-pwa run build
uv run pytest -q
uv run python tests/ui/run.py
uv run python deploy/privacy_check.py
```

The browser suite launches its own loopback Hub and three Sidecars. Install a Chromium build with `npx playwright install chromium` from `apps/web-pwa` or provide Chrome. Some backend integration tests need platform-specific runtimes; check skips on the platform being tested.

For UI changes, include both languages. Application-owned strings go through `uiText` and the English catalog in `apps/web-pwa/src/locales/en.json`. Keep filenames, prompts, user-defined macros, model responses and terminal output intact. Use placeholders for runtime values and test long English labels on a phone viewport.

## Sharing examples

Use loopback, reserved example domains and fictional node names. Copy example configuration into the ignored `local/` directory for personal settings. Keep live environment files, SQLite databases, raw logs, SSH details, registration commands, tokens and conversation exports private. Before submitting a patch, review the diff and run the privacy check. Screenshots require visual inspection because a text scanner cannot examine pixels.

The public guide images are generated from the bilingual browser fixture. Set `FLEET_GUIDE_IMAGES` to `docs/images` when running the browser suite to regenerate them. Legacy UI test captures can be generated locally in `tests/screenshots/`.
