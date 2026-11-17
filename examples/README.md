# AgentBuildKit templates

This directory is the `agentbuildkit-templates` package. The installed import
name is `agentbuildkit_templates`. It contains the Customer Service and
Operations Reporting applications and the local office that registers those
two templates.

The harness imports `agentbuildkit_templates` by that module name. It does
not scan installed packages for other templates. The directories
`customer_service/` and `office/` at this level are checkout wrappers for
`python -m`. They are not installed.

See [docs/install.md](../docs/install.md) for the local wheel install. The
checkout commands in each example README remain available while developing
inside this repository.
