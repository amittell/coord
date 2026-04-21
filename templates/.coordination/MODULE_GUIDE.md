# Modularizing a monolith (incremental)

Goal: create **zones of ownership** so agent teams collide less often.

## Steps

1. Pick the top 1–3 “hot spots” from coordination conflicts / PR merge pain.
2. Create a folder boundary, e.g. `src/modules/<area>/internal/**` vs `src/modules/<area>/api.ts`.
3. Add an ESLint (or language equivalent) boundary rule (see `eslint.restricted-imports.example.cjs`).
4. Update `.coordination/owners.yaml` paths to match reality (broad first, tighten later).
5. Keep **public module APIs** small and stable; route cross-module work through those files.

This is optional for adopting the coordination service, but it is the highest leverage long-term improvement.
