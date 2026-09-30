# Bot de Discord

Bot de Discord que permite a los administradores enviar mensajes mediante `send:`.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `python main.py` — run the Discord bot
- Halloween event data is stored in `data/halloween.json`; `/halloween preparar` creates the reward role and starts the game. The `Administrador`, `Owner` and `Co-Owner` roles can prepare or close the event. The effective Discord permission set `268435504` also grants event management access.
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required secret: `DISCORD_TOKEN` — token del bot de Discord
- Runtime giveaway data is stored in `data/giveaways.json`
- Warning records are stored in `data/warnings.json`; `Owner`, `Co-Owner` and `Mod` can use `warn` and `warns`.

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

_Populate as you build — short repo map plus pointers to the source-of-truth file for DB schema, API contracts, theme files, etc._

## Architecture decisions

_Populate as you build — non-obvious choices a reader couldn't infer from the code (3-5 bullets)._

## Product

- Los administradores pueden escribir `send: mensaje` en un servidor y el bot publica el mensaje.
- Los demás usuarios reciben `No puedes hacer eso tontin` si intentan usar el comando.
- Los administradores pueden crear sorteos con `,sorteo crear <duración> <ganadores> <premio>`.
- Los sorteos también se pueden administrar desde `/giveaway crear`, `/giveaway terminar`, `/giveaway cancelar` y `/giveaway lista`.
- Los participantes entran con un botón y los ganadores se eligen automáticamente al terminar.
- Los sorteos admiten requisitos opcionales, un plazo de reclamación con apertura de ticket y `/giveaway reroll` para el staff.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
