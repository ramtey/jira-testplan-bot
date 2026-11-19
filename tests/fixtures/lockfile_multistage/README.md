# Multi-stage lockfile fixtures

`turbo_prune.Dockerfile` has the shape of the server image behind SK-3060
(plan 721 v1): a global pnpm CLI install in `base` (line 6), `turbo prune
--docker` in `builder` (line 24), and the pruned `out/pnpm-lock.yaml` copied
into `installer` and installed with `pnpm install --frozen-lockfile` (lines
29–31). Line numbers match the original so the citations can be compared.
Registry hosts, image names and the workspace package name are placeholders.

Plan 721 said this image "does not use the committed lockfile". It does.

`no_lockfile.Dockerfile` is the negative case: only `package.json` is copied
and `pnpm install` runs without `--frozen-lockfile`, so the committed lockfile
really is not what ships.
