# Agentless-net targets

`INFRA=agentless-net`. `DEPLOY_SUITE` is `caas` or `bmaas`. The profile is
derived from that choice (`caas-ci` or `bmaas-ci`); do not set `OSAC_PROFILE`.

Root order for a BMaaS install:

```bash
make setup-infra INFRA=agentless-net DEPLOY_SUITE=bmaas
make deploy-ocp INFRA=agentless-net DEPLOY_SUITE=bmaas
make deploy-osac INFRA=agentless-net DEPLOY_SUITE=bmaas
make deploy-suite INFRA=agentless-net DEPLOY_SUITE=bmaas SUITE_STAGE=all
```

`deploy-suite` prepares the fabric and the virtual BMH, validates
`contracts/bmaas`, then runs the one-BMI proof. `SUITE_STAGE=setup` stops
after preparation. `SUITE_STAGE=workload` runs only the proof.

`make run-tests INFRA=agentless-net DEPLOY_SUITE=bmaas` is rejected. That name
is the Netris suite, which needs three BMIs. This lab has one.

The cluster kubeconfig is `~/.kube/<clone>.kubeconfig` (`agentless-lab-mgmt`
by default). A different `KUBECONFIG` fails the boot and the install.

Image overrides, set by the CI build of the selected OSAC commit:

- `FULFILLMENT_SERVICE_IMAGE`
- `OSAC_OPERATOR_IMAGE`
- `OSAC_AAP_IMAGE`
- `BMF_OPERATOR_IMAGE`

`OSAC_REPO` and `OSAC_BRANCH` select the OSAC checkout. Empty means
`https://github.com/osac-project/osac.git` at `main`.

Nightly: `.github/workflows/e2e-bmaas-agentless-net-full-install-caller.yml`,
`15 1 * * *`, Slack only when that schedule fails. Pull requests:
`/test bmaas-agentless-net`.
