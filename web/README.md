# AA Forum web UI

React + Vite + TypeScript + Tailwind. The build output (`web/dist`) is served by the
backend at `/app/`; users running the server do not need Node.

    sh scripts/install-deps.sh   # or: npm ci
    npm run dev                  # http://localhost:5173/app/ , API proxied to AAF_DEV_BACKEND (default http://127.0.0.1:8111)
    npm test                     # component tests (vitest + jsdom)
    npm run build                # type-check + build to dist/

Conventions

- All user-visible text goes through `t()`; add keys to both `src/locales/en.json` and `src/locales/zh-TW.json`
  (a test fails if the key sets differ).
- Message and agent text is rendered as React text nodes only. Never use `dangerouslySetInnerHTML`.
- Colors come from the CSS variables in `src/index.css` (`bg`, `panel`, `sub`, `line`, `fg`, `mute`, `accent`, …);
  don't hard-code hex values in components.
