# SK-3059 fixture: "not deployed" from a comment, after the change shipped

Plan 704 v1, regenerated as plan 747 v2 on 2026-11-19. The repo ships as two
images whose names never mention it: `skysights-api` (built from
`apps/broker-wrapped-api/Dockerfile`) and `skysights-admin-ui`. Nothing in the
deployments repo is named after the repo except the pipeline trigger
(`repoName: broker-wrapped`), so discovery found no deployment source and the
plan took the ticket's comment "#100 is not deployed yet" as the deployment
state:

- `deployment_status` = `not_deployed`, from comments 346224 / 346733;
- happy_path:0, happy_path:3 and edge_cases:0 carried "BLOCKED BY SETUP (rule
  AF): PR #100 is not deployed (comment 346733)";
- the build check said "(web app)", but the change ships in the API container.

At generation time integ-east ran `skysights-api:release1.6.0-7c754c2` and
`skysights-admin-ui:main-aa91729`; both contain #100's merge 8b5eb66
(`git merge-base --is-ancestor`, and Prometheus `kube_pod_container_info`).
Staging and prod still ran `release1.5.0-0e941af` (#99 only).

Sanitized: the org is `example-org`, the deployments repo
`example-org/app-deployments`; hostnames, the image registry, Slack channel
ids, the Okta server id, IAM ARNs and people's names are removed. Image names,
tags, paths, commit ids, PR numbers and comment ids are verbatim.

Used by `tests/test_sk3059_deploy_state_source.py`.
