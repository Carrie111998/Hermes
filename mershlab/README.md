# mershlab/

Everything MershLab builds that isn't a Hermes plugin, skill, or provider
profile lives here, not scattered as new top-level directories that could
be mistaken for something upstream owns.

**The dividing line, stated plainly**: if it fits one of Hermes's own
extension points, it lives there instead, because that's what keeps
`git fetch upstream && git merge upstream/main` a clean fast-forward
(see `MERSHLAB.md` at the repo root, and
`internal-docs/harness/2026-08-20-system-design.md` §2, in the
`MershLab/internal-docs` private repo, not this one):

- A model provider → `plugins/model-providers/<name>/`
- A chat platform → `plugins/platforms/<name>/`
- Agent capability/procedure → `skills/<category>/<name>/`

Everything else that's genuinely ours — process supervision, deployment
scripts, anything with no Hermes-native home — goes here instead:

- `systemd/` — unit and timer templates for running the gateway process
  unattended, supervised, and restarted on crash. Not a Hermes plugin
  because Hermes doesn't have a process-supervision extension point;
  this sits *above* the gateway process, not inside it.

Nothing here is loaded or discovered by Hermes itself — this directory
has no meaning to the running agent, only to a human or a deploy script
setting the whole thing up.
