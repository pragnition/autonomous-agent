"""Which memory scopes a session may read (spec §6.15). Enforced here in code, never by the prompt."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionContext:
    project: str | None = None
    requester: str | None = None
    channel_id: str | None = None
    dm_person: str | None = None
    participants: list[str] = field(default_factory=list)
    person_projects: list[str] = field(default_factory=list)


def scopes_for(ctx: SessionContext) -> list[str]:
    if ctx.dm_person:
        scopes = [f"channels/dm-{ctx.dm_person}", f"people/{ctx.dm_person}", "team"]
        scopes += [f"projects/{p}" for p in ctx.person_projects]
    elif ctx.channel_id:
        scopes = [f"channels/{ctx.channel_id}"]
        scopes += [f"projects/{ctx.project}"] if ctx.project else []
        scopes += ["team"] + [f"people/{p}" for p in ctx.participants]
    else:
        scopes = ([f"projects/{ctx.project}"] if ctx.project else []) + ["team"]
        scopes += [f"people/{ctx.requester}"] if ctx.requester else []
    return list(dict.fromkeys(scopes))
