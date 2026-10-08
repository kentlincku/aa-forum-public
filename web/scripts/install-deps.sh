#!/bin/sh
# One-shot dependency install for the web/ frontend (not a server).
set -e
cd "$(dirname "$0")"
npm i --no-fund --no-audit
npm i --no-fund --no-audit -D tailwindcss @tailwindcss/vite vitest @testing-library/react @testing-library/jest-dom @testing-library/user-event jsdom
npm i --no-fund --no-audit react-router-dom
