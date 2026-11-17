# Contributing

## Send a change upstream

1. Branch from [upstream `develop`](https://github.com/abates/nautobot-app-ssot-unifi/tree/develop).
2. Make one independently reviewable change. Include its regression test and update the relevant docs and lockfile when the change requires them.
3. Test against the versions in upstream CI.
4. Open the pull request against upstream `develop`. Keep private deployment details, live source data, and planning files out of commits and PR descriptions.

Use separate PRs for changes that upstream can accept independently. Identify any
required dependency in the PR description.

## Maintain the fork

Use this fork's `develop` branch for tested integration work. This is the branch
consumers see on GitHub. Follow upstream's `develop` to `main` release workflow.
Keep deployment-specific changes in the deployment repository.

Run the repository's checks before merging. The
[CI workflows](.github/workflows) define the supported runtime and database
matrix.
