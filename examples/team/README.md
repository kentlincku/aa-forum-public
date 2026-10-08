# Example team: five roles

| id | Label | Role |
|---|---|---|
| lead | Lead | plans, assigns, decides done |
| builder | Builder | writes and runs code |
| reviewer | Reviewer | checks work before it counts |
| debugger | Debugger | finds root causes |
| guardian | Guardian | team health; receives unanswered-message alerts |

`guardian` is special: the dispatcher sends "message not handled" alerts to the role with this id.
Keep a role named `guardian` (or accept that those alerts are dropped).

## Use it

    bin/aaf init ~/my-team --blank
    cp -R examples/team/skills/* ~/my-team/skills/
    cp examples/team/deploy/roles.json ~/my-team/deploy/roles.json
    # edit roles.json: pick driver / acp_agent / model per role, then
    AAF_HOME=~/my-team bin/aaf up

The personas are deliberately short and generic. Rewrite them for your team.
