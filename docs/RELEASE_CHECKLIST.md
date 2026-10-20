# Taskbot release checklist

This checklist is for the existing Supabase project and its DEMO data. See [DEPLOYMENT.md](DEPLOYMENT.md) for commands and environment ownership. A checked code item is **not** evidence of a live deployment.

## Codex: prepared locally

- [x] Documented the Vercel Next.js project (`apps/web`), separate HTTPS API, supervised Bot/Worker and server-only environment variables.
- [x] Prepared `0007_restrict_data_api` after `0006_mvp_entities`; its offline test checks RLS and revocation for every mapped Taskbot table. Neither migration was applied to Supabase.
- [x] Documented the private `taskbot-attachments` bucket: 10 MiB, PDF/PNG/JPEG/TXT, server-only key.
- [x] Kept Quick Tunnel configuration local; production Next.js requires a real HTTPS `TASKBOT_API_UPSTREAM`.
- [x] Ran backend, TypeScript and bot/Worker import checks. Attempted production build with the actual local configuration; it correctly rejected the loopback HTTP API upstream. Record the final test counts and local commit hash in the release handoff.

## Owner: required before deployment

- [ ] Provide permanent HTTPS origins for the Mini App and API, with valid DNS/certificates. Configure the API origin in Vercel **Production** as server-only `TASKBOT_API_UPSTREAM` before building.
- [ ] Confirm a completed backup/PITR recovery point for the **existing** Supabase and a restore plan; take and verify an independent export if needed. Preserve all 13 DEMO tasks.
- [ ] Review the project's Data API exposed schemas and approve or revise the `0007` RLS/grant model. Separately authorize migration of the existing DB from `0005` through `0007`; no migration is applied by this checklist.
- [ ] Create the **private** `taskbot-attachments` bucket in that Supabase project with the documented limits. Put its project API origin and server-only secret key in the API's protected environment.
- [ ] Provision a persistent server for FastAPI, one Bot polling process and (later) one Worker process. Set their variables from `.env.example` in the server secret store, without committing credentials.
- [ ] Import `apps/web` as the Vercel project root, set its production HTTPS domain, and run a successful production build with the real API origin.
- [ ] Set the same Mini App URL in the bot environment and BotFather Menu Button; check Telegram `initData` sign-in and account-status denial in Telegram.
- [ ] Review the pending outbox, including DEMO, then separately authorize Worker queue processing and later real Telegram delivery. Keep `TASKBOT_NOTIFICATION_DELIVERY_ENABLED=false` until that decision.
- [ ] Run the production smoke checks in `DEPLOYMENT.md` after deployment. Record the release revision, backup point and rollback/recovery contact.
