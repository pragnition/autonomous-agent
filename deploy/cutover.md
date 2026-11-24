# Switching to agentd

`agent-control` (agentd) replaces `agent-core` and `agent-runner`; `agent-services` keeps the agent's own services.
The Python services stay in `compose.yaml` under the `python` profile, and their images stay built, for a week.

## Before

1. Merge the branch to `main`, pull it into the live checkout.
2. At a quiet time (the build is heavy): `docker compose build control services supervisor supervisor-standby`.
3. `just cutover check`: writes `.env` (the Docker socket's group, the data folder's path), checks the image, lists
   the data folders the switch will hand to the agent user.

## The switch

`just cutover switch`, which:

1. pauses, gives running turns up to 10 minutes (`CUTOVER_IDLE_S`), then stops what still runs (each conversation
   keeps its saved session);
2. backs up `data/db/agent.db` to `data/db/agent-before-control-<time>.db`;
3. stops the supervisors, `agent-core` and `agent-runner`;
4. hands `data/db`, `runs`, `tasks`, `runner` and `cli` to uid 1001 and lets it read `core.env` and `agent.env`
   (sudo);
5. starts `agent-control`, `agent-services` and the supervisors, waits for `/health`, resumes;
6. runs `agentd doctor` and `agentd smoke`.

Then by hand: a DM, a channel mention, a thread reply, a Linear issue, a schedule firing, a spawned agent, every
dashboard page, the kill switch, a meter call through `172.30.0.10:8087`, and `docker restart agent-control` during
a turn (it finishes and its reply posts once).

## Rollback

`just cutover rollback`: stops the supervisors, pauses agentd and stops the conversations' containers, stops
`agent-control` and `agent-services`, starts `agent-core` and `agent-runner` on the same database, points the
supervisors back at them, and resumes. A turn agentd had running is cancelled; its conversation keeps its session.
The tables agentd added stay and the Python services ignore them.

## Rollback drill (during the first week)

1. On a quiet afternoon: `just cutover rollback`; check a DM is answered by the Python runner and the dashboard loads.
2. `just cutover switch` again; check a DM is answered and `just smoke` passes.
3. Note how long each took; a rollback should take under five minutes.

After a week with no rollback: remove the `python` profile services from `compose.yaml` and the old images.
