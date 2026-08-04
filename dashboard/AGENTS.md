<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.

Before writing any UI code, load the `cc-frontend-guidelines` skill
(`.claude/skills/`) — the Computacenter colour/typography/spacing/a11y system is
binding, and it wins over whatever merely looks good. Pure-logic tests run with
`npm test` (node's built-in runner — no jest/vitest).
<!-- END:nextjs-agent-rules -->
